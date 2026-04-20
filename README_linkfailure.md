# SDN Link Failure Detection and Recovery
### Computer Networks – UE24CS252B | Orange Level Project

> **Author:** `<v s r vinay varma>`  
> **SRN:** `<pes1ug24am317>`  
> **Date:** 20-04-2025

---

## Problem Statement

In traditional networks, link failures cause prolonged outages because the control plane (Spanning Tree Protocol) can take 30–50 seconds to converge on an alternate path. This is unacceptable for modern applications.

This project implements an **SDN-based Link Failure Detection and Recovery** system using:
- **Mininet** – virtual network emulator
- **Ryu** – OpenFlow 1.3 SDN controller
- **OpenFlow** – match-action flow rule protocol

The controller:
1. **Monitors** topology changes in real time via OpenFlow port-status and Ryu topology events
2. **Detects** link and switch failures within milliseconds
3. **Updates** flow rules dynamically by deleting stale entries and installing new ones
4. **Restores** connectivity along alternative paths automatically

---

## File Structure

```
sdn_link_failure/
├── link_failure_controller.py   # Ryu controller – detection and recovery logic
├── topology.py                  # Mininet topologies (ring & diamond)
└── README.md                    # This file
```

---

## Setup & Execution

### Prerequisites

Ubuntu 20.04 / 22.04 with Mininet and Ryu installed.

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install mininet -y
pip3 install ryu
sudo mn --version   # verify
```

### Step 1 – Start the Ryu Controller

Open **Terminal 1**:

```bash
ryu-manager link_failure_controller.py ryu.topology.switches
```

You should see:
```
LinkFailureController started
Switch connected | DPID=0000000000000001
```

### Step 2 – Launch the Mininet Topology

Open **Terminal 2**:

```bash
# Ring topology (4 switches in a ring – redundant paths)
sudo python3 topology.py --topo ring

# Or diamond topology (parallel core paths)
sudo python3 topology.py --topo diamond
```

---

## Test Scenarios

### Scenario 1 – Baseline Connectivity
```
mininet> pingall
```
**Expected:** 0% packet loss. Controller installs unicast forwarding rules via BFS shortest path.

### Scenario 2 – Link Failure Simulation
```
mininet> link s1 s2 down
mininet> pingall
```
**Expected in controller logs:**
```
TOPOLOGY | Link DOWN 0000000000000001:1 <-> 0000000000000002:1
RECOVERY | 2 flow(s) affected - computing alternatives
RECOVERY | Alt path for 00:00:00:00:00:01->00:00:00:00:00:03 : s1 -> s4 -> s3
FLOW DELETE  | dpid=0000000000000001 ...
FLOW INSTALL | dpid=0000000000000001 ... out_port=4
```
**Connectivity is restored via the alternate path.**

### Scenario 3 – Link Restoration
```
mininet> link s1 s2 up
mininet> pingall
```
**Expected:** Controller detects link recovery, re-optimises paths back to shortest route.

### Scenario 4 – Flow Table Inspection
```bash
mininet> sh ovs-ofctl dump-flows s1 -O OpenFlow13
```
Shows rules updated dynamically: old paths deleted, new paths installed.

### Scenario 5 – iperf Continuity
```bash
mininet> h4 iperf -s &
mininet> h1 iperf -c 10.0.0.4 -t 30 -i 2
# While running, in another window:
mininet> link s1 s2 down
```
**Expected:** Brief dip in throughput during re-routing (~1s), then recovery.

---

## Expected Output

### Controller Terminal
```
2025-XX [INFO]  Switch connected | DPID=0000000000000001
2025-XX [INFO]  GRAPH | Link added   0000000000000001 port1 <-> 0000000000000002 port1
2025-XX [INFO]  MAC LEARN | 00:00:00:00:00:01 at dpid=0000000000000001 port=1
2025-XX [INFO]  PATH | 00:00:00:00:00:01 -> 00:00:00:00:00:03 : s1 -> s2 -> s3
2025-XX [INFO]  FLOW INSTALL | dpid=0000000000000001 src=... dst=... out_port=2
2025-XX [WARN]  TOPOLOGY | Link DOWN 0000000000000001:1 <-> 0000000000000002:1
2025-XX [WARN]  RECOVERY | 2 flow(s) affected - computing alternatives
2025-XX [INFO]  RECOVERY | Alt path : s1 -> s4 -> s3
2025-XX [INFO]  FLOW DELETE  | dpid=0000000000000001 ...
2025-XX [INFO]  FLOW INSTALL | dpid=0000000000000001 ... out_port=4
```

### Statistics Output (every 10 seconds)
```
========== LINK FAILURE RECOVERY STATISTICS ==========
  Total packet-in       : 62
  Link failures         : 1
  Link recoveries       : 1
  Switch failures       : 0
  Flows re-routed       : 2
  Flow rules installed  : 12
  Flow rules deleted    : 4
=======================================================
```

---

## Architecture – Recovery Flow

```
Link goes down
      |
      v
EventLinkDelete / EventOFPPortStatus
      |
      v
graph.remove_link(src, dst)
      |
      v
_handle_link_failure(src, dst)
      |
      +-- Find active paths using failed link
      |
      +-- For each affected path:
      |       graph.bfs_path(src_dpid, dst_dpid)
      |             |
      |        [Alternative path found?]
      |          YES         NO
      |           |           |
      |   Delete old rules  Log partition
      |   Install new rules
      |
      v
Connectivity restored (~1 second)
```

---

## Flow Rule Design

| Priority | Match | Action | Purpose |
|---|---|---|---|
| 0 | Any (table-miss) | Send to controller | Default – learn and route |
| 10 | eth_src + eth_dst | OUTPUT(port) | Path-based unicast forwarding |
| OFPFC_DELETE | eth_src + eth_dst | – | Remove stale rules on failure |

---

## References

1. Ryu SDN Framework – https://ryu-sdn.org/
2. OpenFlow 1.3 Specification – Open Networking Foundation
3. Mininet Documentation – https://mininet.org/walkthrough/
4. Ryu Topology API – https://ryu.readthedocs.io/en/latest/
5. PES University – UE24CS252B Course Material
