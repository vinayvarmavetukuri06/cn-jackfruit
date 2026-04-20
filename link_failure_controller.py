"""
link_failure_controller.py
==========================
Ryu SDN Controller - Link Failure Detection and Recovery
Computer Networks | UE24CS252B | Orange Level Project

Topic : Link Failure Detection and Recovery
Goal  : Monitor topology changes, detect link failures, update flow
        rules dynamically, and restore connectivity automatically.

Author : <Friend's Name>
SRN    : <Friend's SRN>
Date   : 2025
"""

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (CONFIG_DISPATCHER, MAIN_DISPATCHER,
                                     DEAD_DISPATCHER, set_ev_cls)
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ipv4, arp
from ryu.lib.packet import ether_types
from ryu.lib import hub
from ryu.topology import event as topo_event
from ryu.topology.api import get_switch, get_link
import time
import logging

# ------------------------------------------------------------------------------
#  Configuration Constants
# ------------------------------------------------------------------------------
IDLE_TIMEOUT   = 30      # OpenFlow flow idle timeout (seconds)
HARD_TIMEOUT   = 120     # OpenFlow flow hard timeout (seconds)
RECOVERY_DELAY = 1.0     # seconds to wait before re-routing after failure
STATS_INTERVAL = 10      # periodic stats log interval (seconds)

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(name)s - %(message)s')
log = logging.getLogger('LinkFailureController')


# ------------------------------------------------------------------------------
#  Graph Helper - shortest path via BFS
# ------------------------------------------------------------------------------
class NetworkGraph:
    """
    Lightweight adjacency-list graph of live switches and inter-switch links.
    Uses BFS to compute shortest (hop-count) paths between any two switches.
    """

    def __init__(self):
        # { dpid: { neighbor_dpid: (src_port, dst_port) } }
        self.adjacency = {}
        # { dpid: datapath_object }
        self.datapaths = {}

    def add_switch(self, dpid, datapath):
        if dpid not in self.adjacency:
            self.adjacency[dpid] = {}
        self.datapaths[dpid] = datapath
        log.info("GRAPH | Switch added   dpid=%016x", dpid)

    def remove_switch(self, dpid):
        self.adjacency.pop(dpid, None)
        self.datapaths.pop(dpid, None)
        for nbr in self.adjacency:
            self.adjacency[nbr].pop(dpid, None)
        log.warning("GRAPH | Switch removed dpid=%016x", dpid)

    def add_link(self, src_dpid, dst_dpid, src_port, dst_port):
        self.adjacency.setdefault(src_dpid, {})[dst_dpid] = (src_port, dst_port)
        self.adjacency.setdefault(dst_dpid, {})[src_dpid] = (dst_port, src_port)
        log.info("GRAPH | Link added     %016x port%d <-> %016x port%d",
                 src_dpid, src_port, dst_dpid, dst_port)

    def remove_link(self, src_dpid, dst_dpid):
        removed = False
        if dst_dpid in self.adjacency.get(src_dpid, {}):
            del self.adjacency[src_dpid][dst_dpid]
            removed = True
        if src_dpid in self.adjacency.get(dst_dpid, {}):
            del self.adjacency[dst_dpid][src_dpid]
            removed = True
        if removed:
            log.warning("GRAPH | Link removed   %016x <-> %016x",
                        src_dpid, dst_dpid)
        return removed

    def bfs_path(self, src_dpid, dst_dpid):
        """BFS from src to dst. Returns list of dpids or None if unreachable."""
        if src_dpid == dst_dpid:
            return [src_dpid]
        visited = {src_dpid}
        queue   = [[src_dpid]]
        while queue:
            path    = queue.pop(0)
            current = path[-1]
            for neighbor in self.adjacency.get(current, {}):
                if neighbor not in visited:
                    new_path = path + [neighbor]
                    if neighbor == dst_dpid:
                        return new_path
                    visited.add(neighbor)
                    queue.append(new_path)
        return None

    def get_port(self, src_dpid, dst_dpid):
        """Return egress port on src_dpid toward dst_dpid."""
        return self.adjacency.get(src_dpid, {}).get(dst_dpid, (None,))[0]


# ------------------------------------------------------------------------------
#  Main Controller Application
# ------------------------------------------------------------------------------
class LinkFailureController(app_manager.RyuApp):
    """
    OpenFlow 1.3 controller that:
      1. Builds and maintains a live network topology graph.
      2. Detects link and switch failures via OpenFlow and topology events.
      3. Computes alternative paths using BFS on the updated graph.
      4. Deletes stale flow rules and installs new ones for recovery.
      5. Logs all topology changes, failures, and recovery actions.
    """

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.graph             = NetworkGraph()
        # { mac: (dpid, port) }
        self.mac_to_dpid_port  = {}
        # { (src_mac, dst_mac): [dpid, ...] }
        self.active_paths      = {}

        self.stats = {
            'link_failures'   : 0,
            'link_recoveries' : 0,
            'switch_failures' : 0,
            'reroutes'        : 0,
            'flows_installed' : 0,
            'flows_deleted'   : 0,
            'packet_in_total' : 0,
        }

        self.monitor_thread = hub.spawn(self._stats_logger)
        log.info("LinkFailureController started")

    # --------------------------------------------------------------------------
    #  OpenFlow Handshake
    # --------------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto  = datapath.ofproto
        parser   = datapath.ofproto_parser
        match    = parser.OFPMatch()
        actions  = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                           ofproto.OFPCML_NO_BUFFER)]
        self._add_flow(datapath, priority=0, match=match, actions=actions)
        log.info("Switch connected | DPID=%016x", datapath.id)

    # --------------------------------------------------------------------------
    #  Datapath State Changes (switch connect / disconnect)
    # --------------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPStateChange,
                [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.graph.add_switch(datapath.id, datapath)
        elif ev.state == DEAD_DISPATCHER:
            self.stats['switch_failures'] += 1
            self.graph.remove_switch(datapath.id)
            self._handle_switch_failure(datapath.id)

    # --------------------------------------------------------------------------
    #  Topology Events - inter-switch link up / down
    # --------------------------------------------------------------------------
    @set_ev_cls(topo_event.EventLinkAdd, MAIN_DISPATCHER)
    def link_add_handler(self, ev):
        src = ev.link.src
        dst = ev.link.dst
        self.graph.add_link(src.dpid, dst.dpid, src.port_no, dst.port_no)
        log.info("TOPOLOGY | Link UP   %016x:%d <-> %016x:%d",
                 src.dpid, src.port_no, dst.dpid, dst.port_no)
        self.stats['link_recoveries'] += 1
        self._recompute_all_paths()

    @set_ev_cls(topo_event.EventLinkDelete, MAIN_DISPATCHER)
    def link_delete_handler(self, ev):
        src = ev.link.src
        dst = ev.link.dst
        if self.graph.remove_link(src.dpid, dst.dpid):
            log.warning("TOPOLOGY | Link DOWN %016x:%d <-> %016x:%d",
                        src.dpid, src.port_no, dst.dpid, dst.port_no)
            self.stats['link_failures'] += 1
            hub.sleep(RECOVERY_DELAY)
            self._handle_link_failure(src.dpid, dst.dpid)

    # --------------------------------------------------------------------------
    #  Port Status - additional failure signal
    # --------------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        msg     = ev.msg
        dp      = msg.datapath
        reason  = msg.reason
        port_no = msg.desc.port_no
        ofproto = dp.ofproto

        if reason in (ofproto.OFPPR_DELETE, ofproto.OFPPR_MODIFY):
            log.warning("PORT STATUS | dpid=%016x port=%d reason=%d",
                        dp.id, port_no, reason)
            for neighbor, (src_port, _) in list(
                    self.graph.adjacency.get(dp.id, {}).items()):
                if src_port == port_no:
                    self.graph.remove_link(dp.id, neighbor)
                    self.stats['link_failures'] += 1
                    hub.sleep(RECOVERY_DELAY)
                    self._handle_link_failure(dp.id, neighbor)
                    break

    # --------------------------------------------------------------------------
    #  Packet-In Handler
    # --------------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg      = ev.msg
        datapath = msg.datapath
        ofproto  = datapath.ofproto
        parser   = datapath.ofproto_parser
        in_port  = msg.match['in_port']
        dpid     = datapath.id

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        src_mac = eth.src
        dst_mac = eth.dst
        self.stats['packet_in_total'] += 1

        # MAC Learning
        self.mac_to_dpid_port[src_mac] = (dpid, in_port)
        log.info("MAC LEARN | %s at dpid=%016x port=%d", src_mac, dpid, in_port)

        # Path-based forwarding if destination is known
        if dst_mac in self.mac_to_dpid_port:
            dst_dpid, dst_port = self.mac_to_dpid_port[dst_mac]
            path = self.graph.bfs_path(dpid, dst_dpid)
            if path:
                log.info("PATH | %s -> %s : %s", src_mac, dst_mac,
                         ' -> '.join('%016x' % d for d in path))
                self._install_path(path, src_mac, dst_mac, dst_port, msg)
                self.active_paths[(src_mac, dst_mac)] = path
                return

        # Flood for unknown destinations
        actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
        data    = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        out     = parser.OFPPacketOut(datapath=datapath,
                                      buffer_id=msg.buffer_id,
                                      in_port=in_port,
                                      actions=actions,
                                      data=data)
        datapath.send_msg(out)

    # --------------------------------------------------------------------------
    #  Failure Recovery
    # --------------------------------------------------------------------------
    def _handle_link_failure(self, failed_src, failed_dst):
        """Re-route every active path that traverses the failed link."""
        log.warning("RECOVERY | Handling failure: %016x <-> %016x",
                    failed_src, failed_dst)
        affected = []
        for (sm, dm), path in list(self.active_paths.items()):
            for i in range(len(path) - 1):
                if ({path[i], path[i+1]} == {failed_src, failed_dst}):
                    affected.append((sm, dm))
                    break

        if not affected:
            log.info("RECOVERY | No active flows affected")
            return

        log.warning("RECOVERY | %d flow(s) affected - computing alternatives",
                    len(affected))

        for src_mac, dst_mac in affected:
            if (src_mac not in self.mac_to_dpid_port or
                    dst_mac not in self.mac_to_dpid_port):
                continue
            src_dpid, _        = self.mac_to_dpid_port[src_mac]
            dst_dpid, dst_port = self.mac_to_dpid_port[dst_mac]
            new_path = self.graph.bfs_path(src_dpid, dst_dpid)
            if new_path:
                log.info("RECOVERY | Alt path for %s->%s : %s",
                         src_mac, dst_mac,
                         ' -> '.join('%016x' % d for d in new_path))
                self._delete_path(self.active_paths[(src_mac, dst_mac)],
                                  src_mac, dst_mac)
                self._install_path_rules_only(new_path, src_mac, dst_mac, dst_port)
                self.active_paths[(src_mac, dst_mac)] = new_path
                self.stats['reroutes'] += 1
            else:
                log.error("RECOVERY | No alternative for %s->%s "
                          "(network partitioned)", src_mac, dst_mac)
                del self.active_paths[(src_mac, dst_mac)]

    def _handle_switch_failure(self, failed_dpid):
        """Drop all active paths that pass through the failed switch."""
        to_delete = [k for k, path in self.active_paths.items()
                     if failed_dpid in path]
        for key in to_delete:
            del self.active_paths[key]
            log.warning("RECOVERY | Removed stale path for %s->%s", *key)

    def _recompute_all_paths(self):
        """Optimise paths after a link comes back up."""
        log.info("RECOVERY | Link restored - recomputing paths")
        for (sm, dm), old_path in list(self.active_paths.items()):
            if sm not in self.mac_to_dpid_port or dm not in self.mac_to_dpid_port:
                continue
            src_dpid, _        = self.mac_to_dpid_port[sm]
            dst_dpid, dst_port = self.mac_to_dpid_port[dm]
            new_path = self.graph.bfs_path(src_dpid, dst_dpid)
            if new_path and new_path != old_path:
                self._delete_path(old_path, sm, dm)
                self._install_path_rules_only(new_path, sm, dm, dst_port)
                self.active_paths[(sm, dm)] = new_path
                log.info("RECOVERY | Optimised path for %s->%s", sm, dm)

    # --------------------------------------------------------------------------
    #  Flow Installation / Deletion Helpers
    # --------------------------------------------------------------------------
    def _install_path(self, path, src_mac, dst_mac, dst_port, msg):
        datapath = msg.datapath
        ofproto  = datapath.ofproto
        parser   = datapath.ofproto_parser
        in_port  = msg.match['in_port']

        self._install_path_rules_only(path, src_mac, dst_mac, dst_port)

        out_port = (dst_port if len(path) == 1
                    else self.graph.get_port(path[0], path[1]))
        if out_port is None:
            out_port = ofproto.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]
        data    = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        out     = parser.OFPPacketOut(datapath=datapath,
                                      buffer_id=msg.buffer_id,
                                      in_port=in_port,
                                      actions=actions,
                                      data=data)
        datapath.send_msg(out)

    def _install_path_rules_only(self, path, src_mac, dst_mac, dst_port):
        for i, dpid in enumerate(path):
            if dpid not in self.graph.datapaths:
                continue
            datapath = self.graph.datapaths[dpid]
            parser   = datapath.ofproto_parser
            out_port = (dst_port if i == len(path) - 1
                        else self.graph.get_port(dpid, path[i + 1]))
            if out_port is None:
                continue
            match   = parser.OFPMatch(eth_src=src_mac, eth_dst=dst_mac)
            actions = [parser.OFPActionOutput(out_port)]
            self._add_flow(datapath, priority=10, match=match, actions=actions,
                           idle_timeout=IDLE_TIMEOUT, hard_timeout=HARD_TIMEOUT)
            self.stats['flows_installed'] += 1
            log.info("FLOW INSTALL | dpid=%016x src=%s dst=%s out_port=%d",
                     dpid, src_mac, dst_mac, out_port)

    def _delete_path(self, path, src_mac, dst_mac):
        for dpid in path:
            if dpid not in self.graph.datapaths:
                continue
            datapath = self.graph.datapaths[dpid]
            parser   = datapath.ofproto_parser
            ofproto  = datapath.ofproto
            match    = parser.OFPMatch(eth_src=src_mac, eth_dst=dst_mac)
            mod      = parser.OFPFlowMod(datapath=datapath,
                                          command=ofproto.OFPFC_DELETE,
                                          out_port=ofproto.OFPP_ANY,
                                          out_group=ofproto.OFPG_ANY,
                                          match=match)
            datapath.send_msg(mod)
            self.stats['flows_deleted'] += 1
            log.info("FLOW DELETE  | dpid=%016x src=%s dst=%s", dpid, src_mac, dst_mac)

    def _add_flow(self, datapath, priority, match, actions,
                  idle_timeout=0, hard_timeout=0):
        ofproto = datapath.ofproto
        parser  = datapath.ofproto_parser
        inst    = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod     = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                     match=match, instructions=inst,
                                     idle_timeout=idle_timeout,
                                     hard_timeout=hard_timeout)
        datapath.send_msg(mod)

    # --------------------------------------------------------------------------
    #  Periodic Statistics Logger
    # --------------------------------------------------------------------------
    def _stats_logger(self):
        while True:
            hub.sleep(STATS_INTERVAL)
            log.info(
                "\n"
                "========== LINK FAILURE RECOVERY STATISTICS ==========\n"
                "  Total packet-in       : %d\n"
                "  Link failures         : %d\n"
                "  Link recoveries       : %d\n"
                "  Switch failures       : %d\n"
                "  Flows re-routed       : %d\n"
                "  Flow rules installed  : %d\n"
                "  Flow rules deleted    : %d\n"
                "=======================================================",
                self.stats['packet_in_total'],
                self.stats['link_failures'],
                self.stats['link_recoveries'],
                self.stats['switch_failures'],
                self.stats['reroutes'],
                self.stats['flows_installed'],
                self.stats['flows_deleted'],
            )
