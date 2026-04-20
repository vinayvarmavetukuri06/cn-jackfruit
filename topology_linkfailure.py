"""
topology.py
===========
Mininet Topology for Link Failure Detection and Recovery
Computer Networks | UE24CS252B | Orange Level Project

Two topology scenarios:
  Scenario A : Ring topology   (4 hosts, 4 switches) - redundant paths
  Scenario B : Diamond topology (4 hosts, 4 switches) - multiple alternate routes

Usage:
  # Terminal 1: Start controller
  #   ryu-manager link_failure_controller.py ryu.topology.switches
  #
  # Terminal 2: Start topology
  #   sudo python3 topology.py --topo ring
  #   sudo python3 topology.py --topo diamond

Author : <Friend's Name>
SRN    : <Friend's SRN>
Date   : 2025
"""

import argparse
import time

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.log import setLogLevel, info
from mininet.cli import CLI


# ------------------------------------------------------------------------------
#  Topology A - Ring (4 switches in a ring, 1 host per switch)
# ------------------------------------------------------------------------------
def ring_topology():
    """
    Creates a ring of 4 switches with one host each:

        h1 - s1 --- s2 - h2
               |     |
        h4 - s4 --- s3 - h3

    Two independent paths exist between any pair.
    Failing any single link still leaves one path available.
    """
    info("*** Creating Ring Topology (4 hosts, 4 switches)\n")

    net = Mininet(controller=RemoteController, switch=OVSSwitch,
                  link=TCLink, autoSetMacs=True)

    info("*** Adding Ryu controller (127.0.0.1:6653)\n")
    net.addController('c0', controller=RemoteController,
                      ip='127.0.0.1', port=6653)

    info("*** Adding switches\n")
    s1 = net.addSwitch('s1', protocols='OpenFlow13')
    s2 = net.addSwitch('s2', protocols='OpenFlow13')
    s3 = net.addSwitch('s3', protocols='OpenFlow13')
    s4 = net.addSwitch('s4', protocols='OpenFlow13')

    info("*** Adding hosts\n")
    h1 = net.addHost('h1', ip='10.0.0.1/24')
    h2 = net.addHost('h2', ip='10.0.0.2/24')
    h3 = net.addHost('h3', ip='10.0.0.3/24')
    h4 = net.addHost('h4', ip='10.0.0.4/24')

    info("*** Adding links\n")
    # Hosts to switches
    net.addLink(h1, s1, bw=10)
    net.addLink(h2, s2, bw=10)
    net.addLink(h3, s3, bw=10)
    net.addLink(h4, s4, bw=10)
    # Ring links (redundant paths)
    net.addLink(s1, s2, bw=100)
    net.addLink(s2, s3, bw=100)
    net.addLink(s3, s4, bw=100)
    net.addLink(s4, s1, bw=100)

    info("*** Starting network\n")
    net.start()
    time.sleep(3)

    _run_tests(net, 's1', 's2')
    CLI(net)

    info("*** Stopping network\n")
    net.stop()


# ------------------------------------------------------------------------------
#  Topology B - Diamond (redundant core paths)
# ------------------------------------------------------------------------------
def diamond_topology():
    """
    Diamond topology with redundant core:

              s1 (edge)
             /   \
           s2     s3  (core - two alternative paths)
             \   /
              s4 (edge)
         h1-h2 at s1, h3-h4 at s4

    Failing s2 or its links still allows traffic via s3.
    """
    info("*** Creating Diamond Topology (4 hosts, 4 switches)\n")

    net = Mininet(controller=RemoteController, switch=OVSSwitch,
                  link=TCLink, autoSetMacs=True)

    info("*** Adding Ryu controller (127.0.0.1:6653)\n")
    net.addController('c0', controller=RemoteController,
                      ip='127.0.0.1', port=6653)

    info("*** Adding switches\n")
    s1 = net.addSwitch('s1', protocols='OpenFlow13')
    s2 = net.addSwitch('s2', protocols='OpenFlow13')
    s3 = net.addSwitch('s3', protocols='OpenFlow13')
    s4 = net.addSwitch('s4', protocols='OpenFlow13')

    info("*** Adding hosts\n")
    h1 = net.addHost('h1', ip='10.0.0.1/24')
    h2 = net.addHost('h2', ip='10.0.0.2/24')
    h3 = net.addHost('h3', ip='10.0.0.3/24')
    h4 = net.addHost('h4', ip='10.0.0.4/24')

    info("*** Adding links\n")
    net.addLink(h1, s1, bw=10)
    net.addLink(h2, s1, bw=10)
    net.addLink(h3, s4, bw=10)
    net.addLink(h4, s4, bw=10)
    # Diamond core (two parallel paths s1->s4)
    net.addLink(s1, s2, bw=100)
    net.addLink(s1, s3, bw=100)
    net.addLink(s2, s4, bw=100)
    net.addLink(s3, s4, bw=100)

    info("*** Starting network\n")
    net.start()
    time.sleep(3)

    _run_tests(net, 's1', 's2')
    CLI(net)

    info("*** Stopping network\n")
    net.stop()


# ------------------------------------------------------------------------------
#  Automated Test Scenarios
# ------------------------------------------------------------------------------
def _run_tests(net, link_sw1, link_sw2):
    """
    Scenario 1 : Normal connectivity  (pingall - all hosts reachable)
    Scenario 2 : Link failure         (bring down a link, verify recovery)
    Scenario 3 : Link restoration     (bring link back up)
    Scenario 4 : iperf throughput     (before and after failure)
    """
    hosts = net.hosts
    h1    = net.get('h1')
    h4    = net.get('h4') if 'h4' in [h.name for h in hosts] else hosts[-1]
    sw1   = net.get(link_sw1)
    sw2   = net.get(link_sw2)

    # --- Scenario 1: Baseline connectivity -----------------------------------
    info("\n" + "="*60 + "\n")
    info("  SCENARIO 1: Baseline Connectivity (pingall)\n")
    info("="*60 + "\n")
    net.pingAll()

    # --- Scenario 2: Link failure simulation ---------------------------------
    info("\n" + "="*60 + "\n")
    info("  SCENARIO 2: Link Failure Detection\n")
    info("  Taking down link: %s <-> %s\n" % (link_sw1, link_sw2))
    info("="*60 + "\n")

    # Start background iperf so we can observe continuity
    h4.cmd('iperf -s &')
    time.sleep(1)
    info("  Starting iperf %s -> %s (background)...\n" % (h1.name, h4.name))
    h1.cmd('iperf -c %s -t 30 -i 2 > /tmp/iperf_result.txt &' % h4.IP())
    time.sleep(3)

    # Bring link down
    info("  Bringing down link %s <-> %s ...\n" % (link_sw1, link_sw2))
    sw1.cmd('ifconfig %s-eth%d down' % (link_sw1, 1))
    time.sleep(2)
    info("  Link is DOWN. Controller should detect and re-route...\n")
    time.sleep(3)

    # Verify connectivity still works via alternate path
    info("  Verifying connectivity after failure:\n")
    net.pingAll()

    # --- Scenario 3: Link restoration ----------------------------------------
    info("\n" + "="*60 + "\n")
    info("  SCENARIO 3: Link Restoration\n")
    info("  Bringing link %s <-> %s back UP\n" % (link_sw1, link_sw2))
    info("="*60 + "\n")
    sw1.cmd('ifconfig %s-eth%d up' % (link_sw1, 1))
    time.sleep(3)
    info("  Link restored. Controller should re-optimise paths...\n")
    net.pingAll()

    # --- Scenario 4: Throughput measurement ----------------------------------
    info("\n" + "="*60 + "\n")
    info("  SCENARIO 4: iperf Throughput Result\n")
    info("="*60 + "\n")
    result = h1.cmd('cat /tmp/iperf_result.txt')
    info("  iperf output:\n%s\n" % result)

    # Inspect flow tables
    info("  Flow table on s1:\n")
    info(sw1.cmd('ovs-ofctl dump-flows s1 -O OpenFlow13'))

    info("="*60 + "\n")
    info("  Automated tests complete. Entering Mininet CLI...\n")
    info("  Useful commands:\n")
    info("    pingall\n")
    info("    h1 ping -c4 h4\n")
    info("    sh ovs-ofctl dump-flows s1 -O OpenFlow13\n")
    info("    link s1 s2 down    (simulate failure)\n")
    info("    link s1 s2 up      (restore link)\n")
    info("="*60 + "\n\n")


# ------------------------------------------------------------------------------
#  Entry Point
# ------------------------------------------------------------------------------
if __name__ == '__main__':
    setLogLevel('info')

    parser = argparse.ArgumentParser(
        description='Mininet topology for Link Failure Detection and Recovery')
    parser.add_argument('--topo',
                        choices=['ring', 'diamond'],
                        default='ring',
                        help='Topology: ring (default) or diamond')
    args = parser.parse_args()

    if args.topo == 'diamond':
        diamond_topology()
    else:
        ring_topology()
