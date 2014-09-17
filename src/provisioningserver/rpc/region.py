# Copyright 2014 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""RPC declarations for the region.

These are commands that a region controller ought to respond to.
"""

from __future__ import (
    absolute_import,
    print_function,
    unicode_literals,
    )

str = None

__metaclass__ = type
__all__ = [
    "CreateNode",
    "Identify",
    "MarkNodeFailed",
    "ReportBootImages",
    "UpdateNodePowerState",
]

from provisioningserver.rpc.arguments import (
    Bytes,
    ParsedURL,
    StructureAsJSON,
    )
from provisioningserver.rpc.common import Identify
from provisioningserver.rpc.exceptions import (
    NodeAlreadyExists,
    NoSuchCluster,
    NoSuchEventType,
    NoSuchNode,
    )
from twisted.protocols import amp


class ReportBootImages(amp.Command):
    """Report boot images available on the invoking cluster controller.

    :since: 1.5
    """

    arguments = [
        # The cluster UUID.
        (b"uuid", amp.Unicode()),
        (b"images", amp.AmpList(
            [(b"architecture", amp.Unicode()),
             (b"subarchitecture", amp.Unicode()),
             (b"release", amp.Unicode()),
             (b"purpose", amp.Unicode())])),
    ]
    response = []
    errors = []


class GetBootSources(amp.Command):
    """Report boot sources and selections for the given cluster.

    :since: 1.6
    :deprecated: 1.7
    """

    arguments = [
        # The cluster UUID.
        (b"uuid", amp.Unicode()),
    ]
    response = [
        (b"sources", amp.AmpList(
            [(b"url", amp.Unicode()),
             (b"keyring_data", Bytes()),
             (b"selections", amp.AmpList(
                 [(b"release", amp.Unicode()),
                  (b"arches", amp.ListOf(amp.Unicode())),
                  (b"subarches", amp.ListOf(amp.Unicode())),
                  (b"labels", amp.ListOf(amp.Unicode()))]))])),
    ]
    errors = []


class GetBootSourcesV2(amp.Command):
    """Report boot sources and selections for the given cluster.

    Includes the new os field for the selections.

    :since: 1.7
    """

    arguments = [
        # The cluster UUID.
        (b"uuid", amp.Unicode()),
    ]
    response = [
        (b"sources", amp.AmpList(
            [(b"url", amp.Unicode()),
             (b"keyring_data", Bytes()),
             (b"selections", amp.AmpList(
                 [(b"os", amp.Unicode()),
                  (b"release", amp.Unicode()),
                  (b"arches", amp.ListOf(amp.Unicode())),
                  (b"subarches", amp.ListOf(amp.Unicode())),
                  (b"labels", amp.ListOf(amp.Unicode()))]))])),
    ]
    errors = []


class UpdateLeases(amp.Command):
    """Report DHCP leases on the invoking cluster controller.

    :since: 1.7
    """
    arguments = [
        # The cluster UUID.
        (b"uuid", amp.Unicode()),
        (b"mappings", amp.AmpList(
            [(b"ip", amp.Unicode()),
             (b"mac", amp.Unicode())]))
    ]
    response = []
    errors = []


class GetArchiveMirrors(amp.Command):
    """Return the Main and Port mirrors to use.

    :since: 1.7
    """
    arguments = []
    response = [
        (b"main", ParsedURL()),
        (b"ports", ParsedURL()),
    ]
    errors = []


class GetProxies(amp.Command):
    """Return the HTTP and HTTPS proxies to use.

    :since: 1.6
    """

    arguments = []
    response = [
        (b"http", ParsedURL(optional=True)),
        (b"https", ParsedURL(optional=True)),
    ]
    errors = []


class MarkNodeFailed(amp.Command):
    """Mark a node as 'broken'.

    :since: 1.7
    """

    arguments = [
        # The node's system_id.
        (b"system_id", amp.Unicode()),
        # The error description.
        (b"error_description", amp.Unicode()),
    ]
    response = []
    errors = {NoSuchNode: b"NoSuchNode"}


class ListNodePowerParameters(amp.Command):
    """Return the list of power parameters for nodes
    that this cluster controls.

    Used to query all of the nodes that the cluster
    composes.

    :since: 1.7
    """

    arguments = [
        # The cluster UUID.
        (b"uuid", amp.Unicode()),
    ]
    response = [
        (b"nodes", amp.AmpList(
            [(b"system_id", amp.Unicode()),
             (b"hostname", amp.Unicode()),
             (b"power_state", amp.Unicode()),
             (b"power_type", amp.Unicode()),
             # We can't define a tighter schema here because this is a highly
             # variable bag of arguments from a variety of sources.
             (b"context", StructureAsJSON())])),
    ]
    errors = {
        NoSuchCluster: b"NoSuchCluster",
    }


class UpdateNodePowerState(amp.Command):
    """Update Node Power State.

    :since: 1.7
    """

    arguments = [
        # The node's system_id.
        (b"system_id", amp.Unicode()),
        # The node's power_state.
        (b"power_state", amp.Unicode()),
    ]
    response = []
    errors = {NoSuchNode: b"NoSuchNode"}


class RegisterEventType(amp.Command):
    """Register an event type.

    :since: 1.7
    """

    arguments = [
        (b"name", amp.Unicode()),
        (b"description", amp.Unicode()),
        (b"level", amp.Integer()),
    ]
    response = []
    errors = []


class SendEvent(amp.Command):
    """Send an event.

    :since: 1.7
    """

    arguments = [
        (b"system_id", amp.Unicode()),
        (b"type_name", amp.Unicode()),
        (b"description", amp.Unicode()),
    ]
    response = []
    errors = {
        NoSuchNode: b"NoSuchNode",
        NoSuchEventType: b"NoSuchEventType"
    }


class ReportForeignDHCPServer(amp.Command):
    """Report a foreign DHCP server on the cluster's network.

    :since: 1.7
    """

    arguments = [
        (b"cluster_uuid", amp.Unicode()),
        (b"interface_name", amp.Unicode()),
        (b"foreign_dhcp_ip", amp.Unicode(optional=True)),
    ]
    response = []
    errors = []


class GetClusterInterfaces(amp.Command):
    """Fetch the known interfaces for a cluster from the region.

    :since: 1.7
    """

    arguments = [
        (b"cluster_uuid", amp.Unicode()),
    ]
    response = [
        (b"interfaces", amp.AmpList(
            [(b"name", amp.Unicode()),
             (b"interface", amp.Unicode()),
             (b"ip", amp.Unicode())]))
    ]
    errors = []


class CreateNode(amp.Command):
    """Create a node on a given cluster.

    :since: 1.7
    """

    arguments = [
        (b'cluster_uuid', amp.Unicode()),
        (b'architecture', amp.Unicode()),
        (b'power_type', amp.Unicode()),
        (b'power_parameters', amp.Unicode()),
        (b'mac_addresses', amp.ListOf(amp.Unicode())),
    ]
    response = [
        (b'system_id', amp.Unicode()),
    ]
    errors = {
        NodeAlreadyExists: b"NodeAlreadyExists",
    }


class MonitorExpired(amp.Command):
    """Called by a cluster when a running monitor hits its deadline.

    The original context parameter from the StartMonitors call is returned.

    :since: 1.7
    """

    arguments = [
        (b"id", amp.Unicode()),
        (b"context", StructureAsJSON()),
        ]
    response = []
    errors = []


class ReloadCluster(amp.Command):
    """Called by a cluster when it wants to reload its state.

    The region may respond with many different calls to the cluster
    that will give it all the information it needs to restore state (for
    example when it got restarted).

    For example, the region will restore all the timers in the cluster,
    so none or many StartTimer calls may be received after the cluster
    issues this command.

    :since: 1.7
    """

    arguments = [
        (b"cluster_uuid", amp.Unicode()),
        ]
    response = []
    errors = []
