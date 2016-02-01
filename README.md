Failover of Elastic IP associated with instances in a cluster with Serf
=======================================================================

High availability management of Elastic IPs based on Serf. Serf keeps track
of when instances come and go and will pass those events on to the
`failover-handler.py` script. When new instances join the cluster (which could
also mean that the instance itself is joining some other cluster) script will
take its default elastic ip as specified in `/etc/eip.conf`. When instances fail
or leave the cluster the script will take their default elastic ip so that all
elastic ips are always being served by somebody.

The script only operates in quorum, that is, when it's in the same cluster as
at least half of all the instances. Using quorum helps solve race conditions during netsplits.
