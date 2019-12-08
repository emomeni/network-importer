"""
(c) 2019 Network To Code

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at
  http://www.apache.org/licenses/LICENSE-2.0
Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import sys
import os
import json
import yaml
import logging
import pdb
import re

import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

import requests
import pynetbox
from collections import defaultdict
from pybatfish.client.session import Session
from nornir import InitNornir
from nornir.core.filter import F
from termcolor import colored
from jinja2 import Template, Environment, FileSystemLoader

import network_importer
import network_importer.config as config
from network_importer.utils import TimeTracker, sort_by_digits, timeit
from network_importer.tasks import (
    initialize_devices,
    update_configuration,
    collect_transceivers_info,
    collect_vlans_info,
    device_update_remote,
    check_if_reacheable,
)

from network_importer.model import (
    NetworkImporterDevice,
    NetworkImporterInterface,
    NetworkImporterSite,
    NetworkImporterVlan,
    NetworkImporterOptic,
    Vlan,
    IPAddress,
    Optic,
)

__author__ = "Damien Garros <damien.garros@networktocode.com>"

logger = logging.getLogger("network-importer")


class NetworkImporter(object):
    def __init__(self, check_mode=True):

        self.sites = dict()
        self.devs = None
        self.bf = None
        self.nb = None

        self.check_mode = check_mode

    @timeit
    def build_inventory(self, limit=None):
        """
        Build the inventory for the Network Importer in Nornir format
        # 1/ Devices already exist in Netbox
        #   Case A : configuration are provided
        #   Case B : configuration are not provided but primary IP is defined
        # 
        # 2/ Devices are not in Netbox (Not Supported Yet)
        #   Everything is coming from inventory file
        #                 Mandatory: hostname, platform, username, password

        """

        params = {}

        # ------------------------------------------------------------------------
        # Extract additional query filters if defined and convert string to dict
        #  Filters can be defined at the configuration level or in CLI or both
        # ------------------------------------------------------------------------

        if "inventory_filter" in config.main.keys():
            csparams = config.main["inventory_filter"].split(",")
            for csp in csparams:
                if "=" not in csp:
                    continue

                key, value = csp.split("=", 1)
                params[key] = value

        if limit:
            if "=" not in limit:
                params["name"] = limit

            else:
                csparams = limit.split(",")
                for csp in csparams:
                    if "=" not in csp:
                        continue
                    key, value = csp.split("=", 1)
                    params[key] = value

        if config.main["inventory_source"] == "netbox":
            self.devs = InitNornir(
                core={"num_workers": config.main["nbr_workers"]},
                logging={"enabled": False},
                inventory={
                    "plugin": "network_importer.inventory.NBInventory",
                    "options": {
                        "nb_url": config.netbox["address"],
                        "nb_token": config.netbox["token"],
                        "filter_parameters": params,
                    },
                },
            )

        ## Second option in there but not really supported right now
        elif (
            config.main["inventory_source"] == "configs"
            and "configs_directory" in config.main.keys()
        ):

            ## TODO check if bf session has already been created, otherwise need to create it

            self.devs = InitNornir(
                core={"num_workers": config.main["nbr_workers"]},
                logging={"enabled": False},
                inventory={
                    "plugin": "network_importer.inventory.NornirInventoryFromBatfish",
                    "options": {"devices": self.bf.q.nodeProperties().answer().frame()},
                },
            )

        else:
            logger.critical(
                f"Unable to find an inventory please check the config file and the documentation"
            )
            sys.exit(1)

        return True

    @timeit
    def init(self, limit=None):
        """
        Initilize NetworkImporter Object
            Check if NB is reacheable
            Create inventory 
            Create all NetworkImporterDevice object
            Create all sites

        inputs:
            limit: filter the inventory to limit the execution to a subset of devices
        
        """

        self.check_nb_params()
        self.init_bf_session()
        self.build_inventory(limit=limit)

        # --------------------------------------------------------
        # Creating required directories on local filesystem
        # --------------------------------------------------------
        if (
            not self.check_mode
            and not os.path.exists(config.main["hostvars_directory"])
            and config.main["generate_hostvars"]
        ):
            os.makedirs(config.main["hostvars_directory"])
            logger.debug(
                f"Directory {config.main['hostvars_directory']} was missing, created it"
            )

        # TODO add check to create directory for oper data from devices

        # --------------------------------------------------------
        # Initialize Devices
        #  - Create NID object
        #  - Pull cache information from Netbox
        # --------------------------------------------------------
        results = self.devs.run(task=initialize_devices, bfs=self.bf)

        for dev_name, items in results.items():
            if items[0].failed:
                logger.warning(
                    f"{dev_name} | Something went wrong while trying to initialize the device .. "
                )
                continue

        # --------------------------------------------------------
        # Initialize sites information
        #   Site information are pulled with devices
        #   TODO consider refactoring sites into a Nornir Inventory
        #
        # --------------------------------------------------------
        for dev in self.devs.inventory.hosts.values():

            if not dev.data["obj"].exist_remote:
                continue

            site_slug = dev.data["obj"].remote.site.slug

            ## Check if site and vlans information are already in cache
            if site_slug not in self.sites.keys():
                site = NetworkImporterSite(name=site_slug, nb=self.nb)
                self.sites[site.name] = site
                dev.data["obj"].site = site
                logger.debug(f"Created site {site.name}")

            else:
                dev.data["obj"].site = self.sites[site_slug]

        return True

    @timeit
    def import_devices_from_configs(self):
        """

        """

        # TODO check if bf sessions has been initialized alrealdy
        for host in self.devs.inventory.hosts.values():

            dev = host.data["obj"]

            logger.info(f"Processing {dev.name} data, local and remote .. ")

            bf_ints = self.bf.q.interfaceProperties(nodes=dev.name).answer()

            for bf_intf in bf_ints.frame().itertuples():
                found_intf = False

                intf_name = bf_intf.Interface.interface
                dev.add_batfish_interface(intf_name, bf_intf)

                if config.main["import_ips"]:
                    for prfx in bf_intf.All_Prefixes:
                        dev.add_ip(intf_name, IPAddress(address=prfx))

            if config.main["import_vlans"] == "config":
                bf_vlans = self.bf.q.switchedVlanProperties(nodes=dev.name).answer()
                for vlan in bf_vlans.frame().itertuples():
                    # if vlan.VLAN_ID not in dev.site.vlans.keys():
                    #     dev.site.vlans[vlan.VLAN_ID] = NetworkImporterVlan(site=dev.site.name, vid=vlan.VLAN_ID)

                    dev.site.add_vlan(
                        Vlan(name=f"vlan-{vlan.VLAN_ID}", vid=vlan.VLAN_ID)
                    )

        return True

    @timeit
    def import_devices_from_cmds(self):
        """

        """

        self.devs.filter(filter_func=lambda h: h.data["is_reacheable"]).run(
            task=check_if_reacheable, on_failed=True
        )

        self.warning_devices_not_reacheable()

        if config.main["import_vlans"] == "cli":
            results = self.devs.filter(
                filter_func=lambda h: h.data["is_reacheable"]
            ).run(task=collect_vlans_info, on_failed=True)

            for dev_name, items in results.items():

                if items[0].failed:
                    logger.warning(
                        f" {dev_name} | Something went wrong while trying to pull the vlan information"
                    )
                    continue

                data = items[0].result
                if not "vlans" in data:
                    logger.warning(f" {dev_name} | No vlans informatio returned")
                    continue

                for vlan in data["vlans"].values():
                    if (
                        vlan["vlan_id"]
                        not in self.devs.inventory.hosts[dev_name][
                            "obj"
                        ].site.vlans.keys()
                    ):
                        self.devs.inventory.hosts[dev_name]["obj"].site.add_vlan(
                            NetworkImporterVlan(name=vlan["name"], vid=vlan["vlan_id"])
                        )

                        self.devs.inventory.hosts[dev_name]["obj"].site.add_vlan2(
                            Vlan(name=vlan["name"], vid=vlan["vlan_id"])
                        )

        if config.main["import_transceivers"]:
            # --------------------------------------------- ---
            # Import transceivers information
            # ------------------------------------------------
            results = self.devs.filter(
                filter_func=lambda h: h.data["is_reacheable"]
            ).run(task=collect_transceivers_info, on_failed=True)

            for dev_name, items in results.items():
                if items[0].failed:
                    logger.warning(
                        f" {dev_name} | Something went wrong while trying to pull the transceiver information (1) "
                    )
                    continue

                transceivers = items[0].result

                if not isinstance(transceivers, list):
                    logger.warning(
                        f" {dev_name} | Something went wrong while trying to pull the transceiver information (2)"
                    )
                    continue

                logger.info(f" {dev_name} | Found {len(transceivers)} transceivers")
                for transceiver in transceivers:

                    nio = Optic(
                        name=transceiver["serial"],
                        optic_type=transceiver["type"],
                        intf=transceiver["interface"],
                        serial=transceiver["serial"],
                    )

                    self.devs.inventory.hosts[dev_name].data["obj"].add_optic(
                        intf_name=transceiver["interface"], optic=nio
                    )

        return True

    def get_nb_handler(self):
        """

        """
        if not self.nb:
            self.create_nb_handler()

        return self.nb

    def check_nb_params(self, exit_on_failure=True):
        """
        TODO add support for non exist on failure
        """

        if not self.nb:
            self.create_nb_handler()

        try:
            self.nb.dcim.devices.get(name="notpresent")
        except requests.exceptions.ConnectionError:
            logger.critical(
                f"Unable to connect to the netbox server ({config.netbox['address']})"
            )
            sys.exit(1)
        except pynetbox.core.query.RequestError as e:
            logger.critical(
                f"Unable to complete a query to the netbox server ({config.netbox['address']})"
            )
            print(e)
            sys.exit(1)
        except requests.exceptions.RequestException as e:
            logger.critical(
                f"Unable to connect to the netbox server ({config.netbox['address']}), please check the address and the token"
            )
            print(e)
            sys.exit(1)

        return True

    @timeit
    def update_configurations(self):

        results = self.devs.run(
            task=update_configuration,
            configs_directory=config.main["configs_directory"] + "/configs",
        )

        return True
        # for result in results:

    def warning_devices_not_reacheable(self, msg=""):

        for host in self.devs.filter(
            filter_func=lambda h: h.data["is_reacheable"] == False
        ).inventory.hosts:
            raison = self.devs.inventory.hosts[host].data.get(
                "not_reacheable_raison", "Raison not defined"
            )
            logger.warning(f" {host} device is not reacheable, {raison}")

    def create_nb_handler(self):

        self.nb = pynetbox.api(config.netbox["address"], token=config.netbox["token"])
        return True

    @timeit
    def init_bf_session(self):
        """
        Initialize Batfish 
        TODO Add option to reuse existing snapshot
        """

        # if "configs_directory" not in config.main.keys():
        CURRENT_DIRECTORY = os.getcwd().split("/")[-1]
        NETWORK_NAME = f"network-importer-{CURRENT_DIRECTORY}"
        SNAPSHOT_NAME = "network-importer"
        SNAPSHOT_PATH = config.main["configs_directory"]

        self.bf = Session()
        self.bf.host = config.batfish["address"]
        self.bf.set_network(NETWORK_NAME)

        self.bf.init_snapshot(SNAPSHOT_PATH, name=SNAPSHOT_NAME, overwrite=True)

        return True

    def print_screen(self):
        """
        Print on Screen all devices, interfaces and IPs and how their current status compare to remote
          Currently we only track PRESENT and ABSENT but we should also track DIFF and UPDATED
          This print function might be better off in the device object ...
        """
        PRESENT = colored("PRESENT", "green")
        ABSENT = colored("ABSENT", "yellow")

        for site in self.sites.values():
            print(f" -- Site {site.name} -- ")
            for vlan in site.vlans.values():
                if vlan.exist_remote:
                    print("{:4}{:32}{:12}".format("", f"Vlan {vlan.vid}", PRESENT))
                else:
                    print("{:4}{:32}{:12}".format("", f"Vlan {vlan.vid}", ABSENT))

            print("  ")

            for dev in self.devs.values():
                if dev.site.name != site.name:
                    continue
                if dev.exist_remote:
                    print("{:4}{:42}{:12}".format("", f"Device {dev.name}", PRESENT))
                else:
                    print("{:4}{:42}{:12}".format("", f"Device {dev.name}", ABSENT))

                for intf_name in sorted(dev.interfaces.keys(), key=sort_by_digits):
                    intf = dev.interfaces[intf_name]
                    if intf.exist_remote:
                        print("{:8}{:38}{:12}".format("", f"{intf.name}", PRESENT))
                    else:
                        print("{:8}{:38}{:12}".format("", f"{intf.name}", ABSENT))

                    for ip in intf.ips.values():
                        if ip.exist_remote:
                            print(
                                "{:12}{:34}{:12}".format("", f"{ip.address}", PRESENT)
                            )
                        else:
                            print("{:12}{:34}{:12}".format("", f"{ip.address}", ABSENT))
                print("  ")

        return True

    def diff_local_remote(self):

        for dev_name in self.devs.inventory.hosts.keys():

            diff = self.devs.inventory.hosts[dev_name].data["obj"].diff()

            if diff.has_diffs():
                logger.info(f" {dev_name} is NOT up to date on the remote system")
            else:
                logger.info(f" {dev_name} is up to date")

    @timeit
    def update_remote(self):
        """
        First create all vlans per site to ensure they exist
        """

        for site in self.sites.values():
            site.update_remote()

        results = self.devs.run(task=device_update_remote)

        for dev_name, items in results.items():
            if items[0].failed:
                logger.warning(
                    f" {dev_name} | Something went wrong while to update the device in the remote system"
                )
                continue

        return True

    @timeit
    def import_cabling_from_configs(self):
        """
        Build cabling
          Currently we are only getting the information from the L3 EDGE in Batfish
          We need to pull LLDP data as well using Nornir to complement that
        """

        if not config.main["import_cabling"]:
            return False

        p2p_links = self.bf.q.layer3Edges().answer()
        already_connected_links = {}

        for link in p2p_links.frame().itertuples():

            local_host = link.Interface.hostname
            local_intf = re.sub("\.\d+$", "", link.Interface.interface)
            remote_host = link.Remote_Interface.hostname
            remote_intf = re.sub("\.\d+$", "", link.Remote_Interface.interface)

            unique_id = "_".join(
                sorted([f"{local_host}:{local_intf}", f"{remote_host}:{remote_intf}"])
            )

            try:

                if unique_id in already_connected_links:
                    logger.debug(f"Link {unique_id} already connected .. SKIPPING")
                    continue

                if local_host not in self.devs.inventory.hosts.keys():
                    logger.debug(f"LINK: {local_host} not present in devices list")
                    continue
                elif remote_host not in self.devs.inventory.hosts.keys():
                    logger.debug(f"LINK: {remote_host} not present in devices list")
                    continue

                local_obj = self.devs.inventory.hosts[local_host].data["obj"]
                remote_obj = self.devs.inventory.hosts[remote_host].data["obj"]

                if local_intf not in local_obj.interfaces.keys():
                    logger.warning(
                        f"LINK: {local_host}:{local_intf} not present in interfaces list"
                    )
                    continue
                elif remote_intf not in remote_obj.interfaces.keys():
                    logger.warning(
                        f"LINK: {remote_host}:{remote_intf} not present in interfaces list"
                    )
                    continue

                if local_obj.interfaces[local_intf].is_virtual:
                    logger.debug(
                        f"LINK: {local_host}:{local_intf} is a virtual interface, can't be used for cabling SKIPPING"
                    )
                    continue
                elif remote_obj.interfaces[remote_intf].is_virtual:
                    logger.debug(
                        f"LINK: {remote_host}:{remote_intf} is a virtual interface, can't be used for cabling SKIPPING"
                    )
                    continue

                if not local_obj.interfaces[local_intf].remote:
                    logger.warning(
                        f"LINK: {local_host}:{local_intf} remote object not present SKIPPING"
                    )
                    continue
                elif not remote_obj.interfaces[remote_intf].remote:
                    logger.warning(
                        f"LINK: {remote_host}:{remote_intf} remote object not present SKIPPING"
                    )
                    continue

                ## Check if both interfaces are already connected or not
                if local_obj.interfaces[local_intf].remote.connection_status:
                    remote_host_reported = local_obj.interfaces[
                        local_intf
                    ].remote.connected_endpoint.device.name
                    remote_int_reported = local_obj.interfaces[
                        local_intf
                    ].remote.connected_endpoint.name

                    if remote_host_reported != remote_host:
                        logger.warning(
                            f"LINK: {local_host}:{local_intf} is already connected but to a different device ({remote_host_reported} vs {remote_host})"
                        )
                    elif (
                        remote_host_reported == remote_host
                        and remote_intf != remote_int_reported
                    ):
                        logger.warning(
                            f"LINK: {local_host}:{local_intf} is already connected but to a different interface ({remote_int_reported} vs {remote_intf})"
                        )

                    continue

                elif remote_obj.interfaces[remote_intf].remote.connection_status:
                    local_host_reported = remote_obj.interfaces[
                        remote_intf
                    ].remote.connected_endpoint.device.name
                    local_int_reported = remote_obj.interfaces[
                        remote_intf
                    ].remote.connected_endpoint.name

                    if local_host_reported != local_host:
                        logger.warning(
                            f"LINK: {remote_host}:{remote_intf} is already connected but to a different device ({local_host_reported} vs {local_host})"
                        )
                    elif (
                        local_host_reported == local_host
                        and local_intf != local_int_reported
                    ):
                        logger.warning(
                            f"LINK:  {remote_host}:{remote_intf} is already connected but to a different interface ({local_int_reported} vs {local_intf})"
                        )

                    continue

                else:
                    logger.info(
                        f"Link not present will create it in netbox ({local_host}:{local_intf} || {remote_host}:{remote_intf}) "
                    )
                    link = self.nb.dcim.cables.create(
                        termination_a_type="dcim.interface",
                        termination_a_id=local_obj.interfaces[local_intf].remote.id,
                        termination_b_type="dcim.interface",
                        termination_b_id=remote_obj.interfaces[remote_intf].remote.id,
                    )

                    already_connected_links[unique_id] = 1
            except:
                logger.warning(
                    f"Something went wrong while processing the link {unique_id}",
                    exc_info=True,
                )
