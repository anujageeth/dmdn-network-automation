#!/usr/bin/env python3
"""
configure_snmp.py

Pushes SNMPv2c configuration to EVERY device in inventory.yaml
(routers, SW-CORE, and all distribution/access switches) so Zabbix
can poll them all. Reuses the connection/logging helpers from
configure_routers.py to avoid duplicating logic.

EE8203 - Design and Management of Data Networks
Campus Network Design, Automation & Monitoring
"""

import sys
import logging
from datetime import datetime

from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoTimeoutException, NetmikoAuthenticationException

from configure_routers import load_inventory, build_connection_params, LOG_DIR

# Must match the community string configured on the Zabbix host entry
# for each device (Section 7, step 2).
SNMP_COMMUNITY = "campusRO"

# VM-ZABBIX address (VLAN 40 / DIS, per the host addressing table).
ZABBIX_SERVER_IP = "10.10.40.100"


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"snmp_run_{timestamp}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )
    logging.info("Logging to %s", log_file)


def build_snmp_config():
    """The three SNMP command lines required by the rubric, applied
    to every router and switch."""
    return [
        f"snmp-server community {SNMP_COMMUNITY} RO",
        "snmp-server enable traps",
        f"snmp-server host {ZABBIX_SERVER_IP} version 2c {SNMP_COMMUNITY}",
    ]


def configure_snmp(device, credentials):
    """Connect to one device and push the SNMP config-set."""
    name = device["name"]
    logging.info("=== SNMP: %s (%s) ===", name, device["ip"])
    try:
        params = build_connection_params(device, credentials)
        conn = ConnectHandler(**params)
        conn.enable()

        output = conn.send_config_set(build_snmp_config())
        logging.info("%s: SNMP config pushed.\n%s", name, output)

        conn.save_config()
        conn.disconnect()

    except NetmikoAuthenticationException:
        logging.error("%s: authentication failed - check credentials.", name)
    except NetmikoTimeoutException:
        logging.error("%s: connection timed out - device unreachable or too slow to respond.", name)
    except Exception as exc:  # noqa: BLE001 - log and continue with next device
        logging.error("%s: unexpected error: %s", name, exc)


def main():
    setup_logging()
    data = load_inventory()
    credentials = data["credentials"]

    for device in data["devices"]:
        configure_snmp(device, credentials)


if __name__ == "__main__":
    main()
