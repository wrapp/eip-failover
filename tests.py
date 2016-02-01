failover_handler = __import__('failover-handler')

class TestConfig(object):
    def test_complex_with_eip(self):
        jsondata = {
            'eu-west-1a': {
                'elastic_ip_allocation_id': 'eipalloc-cc618fa9'
            },
            'eu-west-1b': {
                'elastic_ip_allocation_id': 'eipalloc-c5618fa0',
            },
            'eu-west-1c': {
                'elastic_ip_allocation_id': 'eipalloc-c4618fa1',
            }
        }
        conf = failover_handler.Config(jsondata)
        assert conf.elastic_ip_allocation_id('eu-west-1a') == 'eipalloc-cc618fa9'

    def test_complex_without_eip(self):
        jsondata = {
            'eu-west-1a': {
                'route_table_id': 'rtb-0e0ed06b',
            },
            'eu-west-1b': {
                'route_table_id': 'rtb-090ed06c',
            },
            'eu-west-1c': {
                'route_table_id': 'rtb-080ed06d',
            }
        }
        conf = failover_handler.Config(jsondata)
        assert conf.elastic_ip_allocation_id('eu-west-1a') == None
