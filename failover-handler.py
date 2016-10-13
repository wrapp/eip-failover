#!/usr/bin/env python

''' Serf handler for managing failover of AWS EIPs

This handler should be installed on ec2 instances having an EIP. It relies on Serf for
membership events from other eip instances. When other instancess disappear from the cluster
this script will attempt to grab the eip of that instance onto the current instance. It
will only do this if it has quorum, that is, if a majority of the instances are
still available.

Note that on failover, the eip would be associated on eth1 interface otherwise it would
default to eth0 which is public by default.

This script makes certain assumptions:

    * There is a json file called /etc/eip.conf that has a lookup for every zone's
    elastic ip allocation id and ids of the network interfaces as follows:

        {
            "eu-west-1a": {
                "eth1_id": "eni-abc123",
                "eth2_id": "eni-abc456",
                "elastic_ip_allocation_id": "eipalloc-cc618fa9"
            },
            "eu-west-1b": {
                "eth1_id": "eni-def123",
                "eth2_id": "eni-def456",
                "elastic_ip_allocation_id": "eipalloc-c5618fa0",
            },
            "eu-west-1c": {
                "eth1_id": "eni-ghi123",
                "eth2_id": "eni-ghi456",
                "elastic_ip_allocation_id": "eipalloc-c4618fa1",
            }
        }

    * All instances expose the role=eip tag in Serf.
    * All instances instances expose the az tag in Serf, containing the availability
    zone in which it is running. For example, az=eu-west-1a.
    * The jq json processor program is available.

'''

import json
import os
import sys
import time
import socket
import logging

from boto.ec2 import connect_to_region as connect_to_ec2
from boto.vpc import connect_to_region as connect_to_vpc
from boto.utils import get_instance_metadata
from subprocess import call, check_output


ROLE = 'eip'
SERF_PORT = 7946
logger = None


class Config(object):
    def __init__(self, config_dict):
        self.config_dict = config_dict

    def num_zones(self):
        return len(self.config_dict)

    def elastic_ip_allocation_id(self, az):
        entry = self.config_dict[az]
        if isinstance(entry, dict):
            return entry.get('elastic_ip_allocation_id')
        return None

    def eth1_id(self, az):
        entry = self.config_dict[az]
        if isinstance(entry, dict):
            return entry.get('eth1_id')
        return None


class Handler(object):
    def __init__(self, config):
        self.metadata = get_instance_metadata()
        self.config = config
        log('Handler initialized')

    @property
    def current_instance_id(self):
        return self.metadata['instance-id']

    @property
    def current_az(self):
        return self.metadata['placement']['availability-zone']

    @property
    def current_region(self):
        return self.current_az[:-1]

    @property
    def eth0_id(self):
        ec2 = connect_to_ec2(self.current_region)
        instance = ec2.get_only_instances(instance_ids=[self.current_instance_id])[0]
        eth0 = [i for i in instance.interfaces if i.id != self.eth1_id]
        return eth0[0].id

    @property
    def eth1_id(self):
        return self.config.eth1_id(self.current_az)

    def take_elastic_ip(self, az):
        elastic_ip_allocation_id = self.config.elastic_ip_allocation_id(az)
        if elastic_ip_allocation_id:
            ec2 = connect_to_ec2(self.current_region)
            interface_id = self.eth0_id if az == self.current_az else self.eth1_id
            ec2.associate_address(network_interface_id=interface_id,
                                  allocation_id=elastic_ip_allocation_id, allow_reassociation=True)

    def detach_interface(self):
        ec2 = connect_to_ec2(self.current_region)
        interface = ec2.get_all_network_interfaces(filters={'network_interface_id': self.eth1_id})
        if interface and interface[0].status == 'in-use':
            interface[0].detach(True)

    def attach_interface(self):
        device_index = 1
        ec2 = connect_to_ec2(self.current_region)
        interface = ec2.get_all_network_interfaces(filters={'network_interface_id': self.eth1_id})
        if interface and interface[0].status == 'available':
            ec2.attach_network_interface(self.eth1_id, self.current_instance_id, device_index)
            while True:
                call("ifdown --force eth1 2> /dev/null && ifup --force eth1", shell=True)
                dev = check_output('ifconfig | grep -o eth1 || echo ""', shell=True).strip()
                if dev == "eth1":
                    log('eth1 is up')
                    break
                else:
                    log('eth1 is not up. Retrying ...')
                    time.sleep(1)

    def handle(self, az=None):
        az = az or self.current_az
        log('Taking elastic ip from az=%s' % az)
        self.take_elastic_ip(az)


class Quorum(object):
    def __init__(self, config):
        self.config = config
        log('Quorum initialized')

    def quorum(self):
        ''' Returns True if the current nat belongs to the quorum. '''
        t = self.config.num_zones()
        n = t - t / 2
        return self.alive(n)

    def alive(self, n):
        ''' Returns True if at least n nats are alive. '''
        cmd = "serf members -tag role=%s -status=alive -format json | jq '.members | length >= %s' | grep true" % (ROLE, n)
        res = call(cmd, shell=True)
        return res == 0

    __call__ = quorum



class SerfMember(object):
    def __init__(self, hostname, ip, role, tags):
        self.hostname = hostname
        self.ip = ip
        self.role = role
        self.tags = tags

    @property
    def az(self):
        return self.tags['az']

    @classmethod
    def parse_member(cls, row):
        ''' Parses serf row and returns (hostname, ip, role, tags). '''
        hostname, ip, role, tagstr  = row.strip().split('\t')
        tags = cls.parse_tags(tagstr)
        return cls(hostname, ip, role, tags)

    @classmethod
    def parse_tags(cls, tagstr):
        ''' Parses tag strings into a dicts: a=b,c=d -> {a: b, c: d} '''
        pairs = tagstr.split(',')
        return dict([x.split('=') for x in pairs])

    parse = parse_member


def log(message, level=logging.INFO):
    global logger
    if not logger:
        logger = logging.getLogger('serf-handler')
        logger.addHandler(logging.handlers.SysLogHandler(address='/dev/log'))
        logger.setLevel(logging.DEBUG)

    event = os.environ['SERF_EVENT']
    logger.log(level, '[%s-FAILOVER] event=%s, message=%s' % (ROLE, event, message))


def is_member_down(member_ip):
    result = -1
    total_retries = 3
    while total_retries > 0:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex((member_ip, SERF_PORT))
            if result == 0:
                break
        except Exception as ex:
            log("Exception in is_member_down(), error=%s" % str(ex))
        finally:
            log("Retrying, checking if member is down, status_code=%s" % result)
            total_retries -= 1
            time.sleep(1)
    return False if result == 0 else True


def get_serf_members():
    members = map(SerfMember.parse, sys.stdin.readlines())
    members = [x for x in members if x.role == ROLE]
    return members


def main():
    config_location = '/etc/eip.conf'

    with open(config_location) as f:
        config = Config(json.load(f))
    event = os.environ['SERF_EVENT']
    log("Serf-handler called")

    try:
        quorum = Quorum(config)
        handler = Handler(config)
        members = get_serf_members()
        if not members:
            log('No members involved. Ignoring.')
            return

        if not quorum():
            log('No quorum. Cannot failover')
            return

        if event == 'member-join':
            handler.handle()
            handler.detach_interface()
            log('Detach interface done')
        elif event in ['member-leave', 'member-failed']:
            for member in members:
                log("Member reported left/failed, ip=%s, az=%s" % (member.ip, member.az))
                if not is_member_down(member.ip):
                    log("False positive, member is up. Ignoring ...")
                    continue

                log("Confirmed, member is down")
                handler.attach_interface()
                log('Attach interface for az=%s done' % member.az)
                handler.handle(az=member.az)
                log('Elastic ip taken for az=%s done' % member.az)
    except Exception as ex:
        log('Exception in failover logic: %s' % ex, level=logging.ERROR)


if __name__ == '__main__':
    main()
