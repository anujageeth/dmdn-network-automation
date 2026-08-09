#!/usr/bin/env python3
"""
configure_routers.py

Pushes interface IP addressing, OSPF, NAT, and ACL configuration to
R-EDGE, R-CORE, and SW-CORE (the L3 core switch) using Netmiko.

All device data (IPs, interfaces, OSPF networks, NAT rules, ACLs)
is read from inventory.yaml -- nothing is hardcoded here. Credentials
come from environment variables via inventory.yaml's ${VAR} syntax.

EE8203 - Design and Management of Data Networks
Campus Network Design, Automation & Monitoring
"""

import os
import sys
import logging
from datetime import datetime
from pathlib import Path

import yaml
from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoTimeoutException, NetmikoAuthenticationException

INVENTORY_FILE = Path(__file__).parent / "inventory.yaml"
LOG_DIR = Path(__file__).parent / "logs"

# Only these device roles carry router/core-switch style config.
# Distribution/access switches are configured by Ansible instead.
TARGET_ROLES = {"edge_router", "core_router", "core_switch"}


def setup_logging(prefix="run"):
    """Configure logging to both console and a timestamped log file."""
    LOG_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"{prefix}_{timestamp}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logging.info("Logging to %s", log_file)
    return log_file


def load_inventory():
    """Load device inventory + config data, resolving ${VAR} credentials
    from environment variables so no secrets live in this repo."""
    with open(INVENTORY_FILE, "r") as f:
        data = yaml.safe_load(f)

    creds = data["credentials"]
    for key in ("username", "password", "enable_secret"):
        if key in creds and creds[key]:
            creds[key] = os.path.expandvars(creds[key])
            if creds[key].startswith("${") or not creds[key]:
                logging.warning(
                    "Environment variable for '%s' is not set - "
                    "export it before running this script.", key
                )
    return data


def build_connection_params(device, credentials):
    """Build the Netmiko device dictionary for a single device.

    Dynamips-emulated c7200/c3725 nodes are software-emulated and
    respond more slowly over SSH than QEMU-based images would, so
    timeout is raised and fast_cli is disabled to avoid Netmiko
    misreading a slow-but-healthy device as unreachable.
    """
    return {
        "device_type": device["device_type"],
        "host": device["ip"],
        "username": credentials["username"],
        "password": credentials["password"],
        "secret": credentials.get("enable_secret") or credentials["password"],
        "timeout": 60,
        "fast_cli": False,
    }


def connect_device(device, credentials):
    """Open a Netmiko SSH connection to one device and enter enable mode."""
    params = build_connection_params(device, credentials)
    conn = ConnectHandler(**params)
    conn.enable()
    return conn


def build_interface_config(device):
    """Build interface IP addressing commands for routed interfaces."""
    lines = []
    for intf in device.get("interfaces", []):
        lines.append(f"interface {intf['name']}")
        if intf.get("description"):
            lines.append(f" description {intf['description']}")
        lines.append(f" ip address {intf['ip']} {intf['mask']}")
        lines.append(" no shutdown")
    return lines


def build_svi_config(device):
    """Build SVI (inter-VLAN routing) and routed-uplink commands for SW-CORE."""
    lines = []
    for svi in device.get("svis", []):
        lines.append(f"interface Vlan{svi['vlan']}")
        lines.append(f" ip address {svi['ip']} {svi['mask']}")
        lines.append(" no shutdown")

    routed = device.get("routed_interface")
    if routed:
        lines.append(f"interface {routed['name']}")
        if routed.get("description"):
            lines.append(f" description {routed['description']}")
        lines.append(f" ip address {routed['ip']} {routed['mask']}")
        lines.append(" no shutdown")

    return lines


def push_ospf(device):
    """Build OSPF area 0 network statements.

    Cisco IOS 'router ospf' / 'network ... area 0' statements are
    naturally idempotent: re-issuing an identical network statement
    does not duplicate or change device state, so re-running this
    script is safe (documented in the report's automation-tool
    justification section).
    """
    ospf = device.get("ospf")
    if not ospf:
        return []
    lines = [f"router ospf {ospf['process_id']}"]
    for net in ospf["networks"]:
        lines.append(f" network {net['network']} {net['wildcard']} area {net['area']}")
    return lines


def push_nat(device):
    """Build NAT overload + default route configuration (R-EDGE only)."""
    nat = device.get("nat")
    if not nat:
        return []

    lines = [f"ip access-list standard {nat['inside_source_acl']}"]
    for rule in nat["acl_rules"]:
        lines.append(f" {rule}")

    for intf_name in nat.get("inside_interfaces", []):
        lines.append(f"interface {intf_name}")
        lines.append(" ip nat inside")

    lines.append(f"interface {nat['outside_interface']}")
    lines.append(" ip nat outside")
    lines.append(
        f"ip nat inside source list {nat['inside_source_acl']} "
        f"interface {nat['outside_interface']} overload"
    )

    default_route = device.get("default_route")
    if default_route:
        lines.append(f"ip route 0.0.0.0 0.0.0.0 {default_route['next_hop']}")

    return lines


def push_acl(device):
    """Build extended named ACLs and bind them inbound on SW-CORE SVIs
    (Section 4: ACLs are applied at the inter-VLAN routing point)."""
    lines = []
    for acl in device.get("acls", []):
        lines.append(f"ip access-list extended {acl['name']}")
        for rule in acl["rules"]:
            lines.append(f" {rule}")
        lines.append(f"interface Vlan{acl['apply_to_svi']}")
        lines.append(f" ip access-group {acl['name']} {acl['direction']}")
    return lines


def build_config_set(device):
    """Assemble the full ordered config-set to push to one device."""
    config = []
    config += build_interface_config(device)
    config += build_svi_config(device)
    config += push_ospf(device)
    config += push_nat(device)
    config += push_acl(device)
    return config


def configure_device(device, credentials):
    """Connect to one device, push its config-set, and log the result."""
    name = device["name"]
    logging.info("=== %s (%s) ===", name, device["ip"])

    config_set = build_config_set(device)
    if not config_set:
        logging.info("%s: no router/core-switch config defined, skipping.", name)
        return

    try:
        conn = connect_device(device, credentials)
        output = conn.send_config_set(config_set)
        logging.info("%s: config pushed successfully.\n%s", name, output)

        save_output = conn.save_config()
        logging.info("%s: configuration saved.\n%s", name, save_output)

        conn.disconnect()

    except NetmikoAuthenticationException:
        logging.error("%s: authentication failed - check credentials.", name)
    except NetmikoTimeoutException:
        logging.error("%s: connection timed out - device unreachable or too slow to respond.", name)
    except Exception as exc:  # noqa: BLE001 - log and continue with next device
        logging.error("%s: unexpected error: %s", name, exc)


def main():
    setup_logging(prefix="run")
    data = load_inventory()
    credentials = data["credentials"]

    for device in data["devices"]:
        if device.get("role") in TARGET_ROLES:
            configure_device(device, credentials)
        else:
            logging.info(
                "%s: role '%s' is handled by Ansible, skipping.",
                device["name"], device.get("role"),
            )


if __name__ == "__main__":
    main()
