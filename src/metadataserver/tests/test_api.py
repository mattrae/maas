# Copyright 2012-2017 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the metadata API."""

__all__ = []

import base64
from collections import namedtuple
from datetime import datetime
import http.client
from io import BytesIO
import json
from math import (
    ceil,
    floor,
)
from operator import itemgetter
import os.path
import random
import tarfile
import time
from unittest.mock import (
    ANY,
    Mock,
)

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.urlresolvers import reverse
from maasserver import preseed as preseed_module
from maasserver.clusterrpc.testing.boot_images import make_rpc_boot_image
from maasserver.enum import (
    NODE_STATUS,
    NODE_TYPE,
    NODE_TYPE_CHOICES,
    POWER_STATE,
)
from maasserver.exceptions import (
    MAASAPINotFound,
    Unauthorized,
)
from maasserver.models import (
    Event,
    SSHKey,
    Tag,
    VersionedTextFile,
)
from maasserver.models.node import Node
from maasserver.models.signals.testing import SignalsDisabled
from maasserver.rpc.testing.mixins import PreseedRPCMixin
from maasserver.testing.config import RegionConfigurationFixture
from maasserver.testing.factory import factory
from maasserver.testing.matchers import HasStatusCode
from maasserver.testing.testcase import (
    MAASServerTestCase,
    MAASTransactionServerTestCase,
)
from maasserver.testing.testclient import MAASSensibleOAuthClient
from maasserver.utils.orm import reload_object
from maastesting.matchers import (
    MockCalledOnceWith,
    MockNotCalled,
)
from maastesting.utils import sample_binary_data
from metadataserver import api
from metadataserver.api import (
    add_event_to_node_event_log,
    check_version,
    get_node_for_mac,
    get_node_for_request,
    get_queried_node,
    make_list_response,
    make_text_response,
    MetaDataHandler,
    process_file,
    UnknownMetadataVersion,
)
from metadataserver.enum import (
    SCRIPT_STATUS,
    SCRIPT_STATUS_CHOICES,
    SCRIPT_TYPE,
    SIGNAL_STATUS,
)
from metadataserver.models import (
    NodeKey,
    NodeUserData,
)
from metadataserver.nodeinituser import get_node_init_user
from netaddr import IPNetwork
from provisioningserver.events import (
    EVENT_DETAILS,
    EVENT_TYPES,
)
from provisioningserver.refresh.node_info_scripts import NODE_INFO_SCRIPTS
from testtools.matchers import (
    Contains,
    ContainsAll,
    ContainsDict,
    Equals,
    HasLength,
    KeysEqual,
    MatchesAll,
    Not,
    StartsWith,
)
import yaml


LooksLikeCloudInit = ContainsDict(
    {"cloud-init": StartsWith("#cloud-config")})


class TestHelpers(MAASServerTestCase):
    """Tests for the API helper functions."""

    def fake_request(self, **kwargs):
        """Produce a cheap fake request, fresh from the sweat shop.

        Pass as arguments any header items you want to include.
        """
        return namedtuple('FakeRequest', ['META'])(kwargs)

    def test_make_text_response_presents_text_as_text_plain(self):
        input_text = "Hello."
        response = make_text_response(input_text)
        self.assertEqual('text/plain', response['Content-Type'])
        self.assertEqual(
            input_text, response.content.decode(settings.DEFAULT_CHARSET))

    def test_make_list_response_presents_list_as_newline_separated_text(self):
        response = make_list_response(['aaa', 'bbb'])
        self.assertEqual('text/plain', response['Content-Type'])
        self.assertEqual(
            "aaa\nbbb", response.content.decode(settings.DEFAULT_CHARSET))

    def test_check_version_accepts_latest(self):
        check_version('latest')
        # The test is that we get here without exception.
        pass

    def test_check_version_reports_unknown_version(self):
        self.assertRaises(UnknownMetadataVersion, check_version, '2.0')

    def test_get_node_for_request_finds_node(self):
        node = factory.make_Node()
        token = NodeKey.objects.get_token_for_node(node)
        request = self.fake_request(
            HTTP_AUTHORIZATION=factory.make_oauth_header(
                oauth_token=token.key))
        self.assertEqual(node, get_node_for_request(request))

    def test_get_node_for_request_reports_missing_auth_header(self):
        self.assertRaises(
            Unauthorized,
            get_node_for_request, self.fake_request())

    def test_get_node_for_mac_refuses_if_anonymous_access_disabled(self):
        self.patch(settings, 'ALLOW_UNSAFE_METADATA_ACCESS', False)
        self.assertRaises(
            PermissionDenied, get_node_for_mac, factory.make_mac_address())

    def test_get_node_for_mac_raises_404_for_unknown_mac(self):
        self.assertRaises(
            MAASAPINotFound, get_node_for_mac, factory.make_mac_address())

    def test_get_node_for_mac_finds_node_by_mac(self):
        node = factory.make_Node_with_Interface_on_Subnet()
        iface = node.get_boot_interface()
        self.assertEqual(iface.node, get_node_for_mac(iface.mac_address))

    def test_get_queried_node_looks_up_by_mac_if_given(self):
        node = factory.make_Node_with_Interface_on_Subnet()
        iface = node.get_boot_interface()
        self.assertEqual(
            iface.node,
            get_queried_node(object(), for_mac=iface.mac_address))

    def test_get_queried_node_looks_up_oauth_key_by_default(self):
        node = factory.make_Node()
        token = NodeKey.objects.get_token_for_node(node)
        request = self.fake_request(
            HTTP_AUTHORIZATION=factory.make_oauth_header(
                oauth_token=token.key))
        self.assertEqual(node, get_queried_node(request))

    def test_add_event_to_node_event_log(self):
        expected_type = {
            # These statuses have specific event types.
            NODE_STATUS.COMMISSIONING: EVENT_TYPES.NODE_COMMISSIONING_EVENT,
            NODE_STATUS.DEPLOYING: EVENT_TYPES.NODE_INSTALL_EVENT,

            # All other statuses generate NODE_STATUS_EVENT events.
            NODE_STATUS.NEW: EVENT_TYPES.NODE_STATUS_EVENT,
            NODE_STATUS.FAILED_COMMISSIONING: EVENT_TYPES.NODE_STATUS_EVENT,
            NODE_STATUS.MISSING: EVENT_TYPES.NODE_STATUS_EVENT,
            NODE_STATUS.READY: EVENT_TYPES.NODE_STATUS_EVENT,
            NODE_STATUS.RESERVED: EVENT_TYPES.NODE_STATUS_EVENT,
            NODE_STATUS.ALLOCATED: EVENT_TYPES.NODE_STATUS_EVENT,
            NODE_STATUS.DEPLOYED: EVENT_TYPES.NODE_STATUS_EVENT,
            NODE_STATUS.RETIRED: EVENT_TYPES.NODE_STATUS_EVENT,
            NODE_STATUS.BROKEN: EVENT_TYPES.NODE_STATUS_EVENT,
            NODE_STATUS.FAILED_DEPLOYMENT: EVENT_TYPES.NODE_STATUS_EVENT,
            NODE_STATUS.RELEASING: EVENT_TYPES.NODE_STATUS_EVENT,
            NODE_STATUS.FAILED_RELEASING: EVENT_TYPES.NODE_STATUS_EVENT,
            NODE_STATUS.DISK_ERASING: EVENT_TYPES.NODE_STATUS_EVENT,
            NODE_STATUS.FAILED_DISK_ERASING: EVENT_TYPES.NODE_STATUS_EVENT,
        }

        for status in expected_type:
            node = factory.make_Node(status=status)
            origin = factory.make_name('origin')
            action = factory.make_name('action')
            description = factory.make_name('description')
            add_event_to_node_event_log(node, origin, action, description)
            event = Event.objects.get(node=node)

            self.assertEqual(node, event.node)
            self.assertEqual(action, event.action)
            self.assertIn(origin, event.description)
            self.assertIn(description, event.description)
            self.assertEqual(expected_type[node.status], event.type.name)

    def test_add_event_to_node_event_log_logs_rack_refresh(self):
        rack = factory.make_RackController()
        origin = factory.make_name('origin')
        action = factory.make_name('action')
        description = factory.make_name('description')
        add_event_to_node_event_log(rack, origin, action, description)
        event = Event.objects.get(node=rack)

        self.assertEqual(rack, event.node)
        self.assertEqual(action, event.action)
        self.assertIn(origin, event.description)
        self.assertIn(description, event.description)
        self.assertEqual(
            EVENT_TYPES.REQUEST_CONTROLLER_REFRESH, event.type.name)

    def test_process_file_creates_new_entry_for_output(self):
        results = {}
        script_result = factory.make_ScriptResult(status=SCRIPT_STATUS.RUNNING)
        output = factory.make_string()
        request = {'exit_status': random.randint(0, 255)}

        process_file(
            results, script_result.script_set, script_result.name, output,
            request)

        self.assertDictEqual(
            {
                'exit_status': request['exit_status'],
                'output': output,
            },
            results[script_result])

    def test_process_file_creates_adds_field_for_output(self):
        results = {}
        script_result = factory.make_ScriptResult(status=SCRIPT_STATUS.RUNNING)
        output = factory.make_string()
        stdout = factory.make_string()
        stderr = factory.make_string()
        request = {'exit_status': random.randint(0, 255)}

        process_file(
            results, script_result.script_set, '%s.out' % script_result.name,
            stdout, request)
        process_file(
            results, script_result.script_set, '%s.err' % script_result.name,
            stderr, request)
        process_file(
            results, script_result.script_set, script_result.name, output,
            request)

        self.assertDictEqual(
            {
                'exit_status': request['exit_status'],
                'output': output,
                'stdout': stdout,
                'stderr': stderr,
            },
            results[script_result])

    def test_process_file_creates_new_entry_for_stdout(self):
        results = {}
        script_result = factory.make_ScriptResult(status=SCRIPT_STATUS.RUNNING)
        stdout = factory.make_string()
        request = {'exit_status': random.randint(0, 255)}

        process_file(
            results, script_result.script_set, '%s.out' % script_result.name,
            stdout, request)

        self.assertDictEqual(
            {
                'exit_status': request['exit_status'],
                'stdout': stdout,
            },
            results[script_result])

    def test_process_file_creates_adds_field_for_stdout(self):
        results = {}
        script_result = factory.make_ScriptResult(status=SCRIPT_STATUS.RUNNING)
        output = factory.make_string()
        stdout = factory.make_string()
        stderr = factory.make_string()
        request = {'exit_status': random.randint(0, 255)}

        process_file(
            results, script_result.script_set, script_result.name, output,
            request)
        process_file(
            results, script_result.script_set, '%s.err' % script_result.name,
            stderr, request)
        process_file(
            results, script_result.script_set, '%s.out' % script_result.name,
            stdout, request)

        self.assertDictEqual(
            {
                'exit_status': request['exit_status'],
                'output': output,
                'stdout': stdout,
                'stderr': stderr,
            },
            results[script_result])

    def test_process_file_creates_new_entry_for_stderr(self):
        results = {}
        script_result = factory.make_ScriptResult(status=SCRIPT_STATUS.RUNNING)
        stderr = factory.make_string()
        request = {'exit_status': random.randint(0, 255)}

        process_file(
            results, script_result.script_set, '%s.err' % script_result.name,
            stderr, request)

        self.assertDictEqual(
            {
                'exit_status': request['exit_status'],
                'stderr': stderr,
            },
            results[script_result])

    def test_process_file_creates_adds_field_for_stderr(self):
        results = {}
        script_result = factory.make_ScriptResult(status=SCRIPT_STATUS.RUNNING)
        output = factory.make_string()
        stdout = factory.make_string()
        stderr = factory.make_string()
        request = {'exit_status': random.randint(0, 255)}

        process_file(
            results, script_result.script_set, script_result.name, output,
            request)
        process_file(
            results, script_result.script_set, '%s.out' % script_result.name,
            stdout, request)
        process_file(
            results, script_result.script_set, '%s.err' % script_result.name,
            stderr, request)

        self.assertDictEqual(
            {
                'exit_status': request['exit_status'],
                'output': output,
                'stdout': stdout,
                'stderr': stderr,
            },
            results[script_result])

    def test_process_file_finds_script_result_by_id(self):
        results = {}
        script_result = factory.make_ScriptResult(status=SCRIPT_STATUS.RUNNING)
        output = factory.make_string()
        request = {
            'exit_status': random.randint(0, 255),
            'script_result_id': script_result.id,
        }

        process_file(
            results, script_result.script_set,
            factory.make_name('script_name'), output, request)

        self.assertDictEqual(
            {
                'exit_status': request['exit_status'],
                'output': output,
            },
            results[script_result])

    def test_process_file_adds_script_version_id(self):
        results = {}
        script_result = factory.make_ScriptResult(status=SCRIPT_STATUS.RUNNING)
        output = factory.make_string()
        request = {
            'exit_status': random.randint(0, 255),
            'script_result_id': script_result.id,
            'script_version_id': script_result.script.script_id,
        }

        process_file(
            results, script_result.script_set,
            factory.make_name('script_name'), output, request)

        self.assertDictEqual(
            {
                'exit_status': request['exit_status'],
                'output': output,
                'script_version_id': script_result.script.script_id,
            },
            results[script_result])

    def test_creates_new_script_result(self):
        results = {}
        script_name = factory.make_name('script_name')
        script_set = factory.make_ScriptSet()
        output = factory.make_string()
        request = {'exit_status': random.randint(0, 255)}

        process_file(results, script_set, script_name, output, request)
        script_result, value = list(results.items())[0]

        self.assertEquals(script_name, script_result.name)
        self.assertEquals(SCRIPT_STATUS.RUNNING, script_result.status)
        self.assertDictEqual(
            {
                'exit_status': request['exit_status'],
                'output': output,
            },
            value)

    def test_stores_script_version(self):
        results = {}
        script_name = factory.make_name('script_name')
        script = factory.make_Script()
        script.script = script.script.update(factory.make_string())
        script_set = factory.make_ScriptSet()
        output = factory.make_string()
        request = {
            'exit_status': random.randint(0, 255),
            'script_version_id': script.script.id,
        }

        process_file(results, script_set, script_name, output, request)

        script_result, value = list(results.items())[0]

        self.assertEquals(script_name, script_result.name)
        self.assertDictEqual(
            {
                'exit_status': request['exit_status'],
                'output': output,
                'script_version_id': script.script.id,
            },
            value)


def make_node_client(node=None):
    """Create a test client logged in as if it were `node`."""
    if node is None:
        node = factory.make_Node()
    token = NodeKey.objects.get_token_for_node(node)
    return MAASSensibleOAuthClient(get_node_init_user(), token)


def call_signal(
        client=None, version='latest', files: dict=None, headers: dict=None,
        status=None, **kwargs):
    """Call the API's signal method.

    :param client: Optional client to POST with.  If omitted, will create
        one for a commissioning node.
    :param version: API version to post on.  Defaults to "latest".
    :param files: Optional dict of files to attach.  Maps file name to
        file contents.
    :param **kwargs: Any other keyword parameters are passed on directly
        to the "signal" call.
    """
    if files is None:
        files = {}
    if headers is None:
        headers = {}
    if client is None:
        client = make_node_client(factory.make_Node(
            status=NODE_STATUS.COMMISSIONING))
    if status is None:
        status = SIGNAL_STATUS.OK
    params = {
        'op': 'signal',
        'status': status,
    }
    params.update(kwargs)
    params.update({
        name: factory.make_file_upload(name, content)
        for name, content in files.items()
    })
    url = reverse('metadata-version', args=[version])
    return client.post(url, params, **headers)


class TestMetadataCommon(MAASServerTestCase):
    """Tests for the common metadata/curtin-metadata API views."""

    # The curtin-metadata and the metadata views are similar in every
    # aspect except the user-data end-point.  The same tests are used to
    # test both end-points.
    scenarios = [
        ('metadata', {'metadata_prefix': 'metadata'}),
        ('curtin-metadata', {'metadata_prefix': 'curtin-metadata'}),
    ]

    def get_metadata_name(self, name_suffix=''):
        """Return the Django name of the metadata view.

        :param name_suffix: Suffix of the view name.  The default value is
            the empty string (get_metadata_name() will return the root of
            the metadata API in this case).

        Depending on the value of self.metadata_prefix, this will return
        the name of the metadata view or of the curtin-metadata view.
        """
        return self.metadata_prefix + name_suffix

    def test_no_anonymous_access(self):
        url = reverse(self.get_metadata_name())
        self.assertEqual(
            http.client.UNAUTHORIZED, self.client.get(url).status_code)

    def test_metadata_index_shows_latest(self):
        client = make_node_client()
        url = reverse(self.get_metadata_name())
        content = client.get(url).content.decode(settings.DEFAULT_CHARSET)
        self.assertIn('latest', content)

    def test_metadata_index_shows_only_known_versions(self):
        client = make_node_client()
        url = reverse(self.get_metadata_name())
        content = client.get(url).content.decode(settings.DEFAULT_CHARSET)
        for item in content.splitlines():
            check_version(item)
        # The test is that we get here without exception.
        pass

    def test_version_index_shows_unconditional_entries(self):
        client = make_node_client()
        view_name = self.get_metadata_name('-version')
        url = reverse(view_name, args=['latest'])
        content = client.get(url).content.decode(settings.DEFAULT_CHARSET)
        self.assertThat(content.splitlines(), ContainsAll([
            'meta-data',
            'maas-commissioning-scripts',
            ]))

    def test_version_index_does_not_show_user_data_if_not_available(self):
        client = make_node_client()
        view_name = self.get_metadata_name('-version')
        url = reverse(view_name, args=['latest'])
        content = client.get(url).content.decode(settings.DEFAULT_CHARSET)
        self.assertNotIn('user-data', content.splitlines())

    def test_version_index_shows_user_data_if_available(self):
        node = factory.make_Node()
        NodeUserData.objects.set_user_data(node, b"User data for node")
        client = make_node_client(node)
        view_name = self.get_metadata_name('-version')
        url = reverse(view_name, args=['latest'])
        content = client.get(url).content.decode(settings.DEFAULT_CHARSET)
        self.assertIn('user-data', content.splitlines())

    def test_meta_data_view_lists_fields(self):
        # Some fields only are returned if there is data related to them.
        user, _ = factory.make_user_with_keys(n_keys=2, username='my-user')
        node = factory.make_Node(owner=user)
        client = make_node_client(node=node)
        view_name = self.get_metadata_name('-meta-data')
        url = reverse(view_name, args=['latest', ''])
        response = client.get(url)
        self.assertIn('text/plain', response['Content-Type'])
        self.assertItemsEqual(
            MetaDataHandler.subfields, [
                field.decode(settings.DEFAULT_CHARSET)
                for field in response.content.split()
            ])

    def test_meta_data_view_is_sorted(self):
        client = make_node_client()
        view_name = self.get_metadata_name('-meta-data')
        url = reverse(view_name, args=['latest', ''])
        response = client.get(url)
        attributes = response.content.split()
        self.assertEqual(sorted(attributes), attributes)

    def test_meta_data_unknown_item_is_not_found(self):
        client = make_node_client()
        view_name = self.get_metadata_name('-meta-data')
        url = reverse(view_name, args=['latest', 'UNKNOWN-ITEM'])
        response = client.get(url)
        self.assertEqual(http.client.NOT_FOUND, response.status_code)

    def test_get_attribute_producer_supports_all_fields(self):
        handler = MetaDataHandler()
        producers = list(map(handler.get_attribute_producer, handler.fields))
        self.assertNotIn(None, producers)

    def test_meta_data_local_hostname_returns_fqdn(self):
        hostname = factory.make_string()
        domain = factory.make_Domain()
        node = factory.make_Node(
            hostname='%s.%s' % (hostname, domain.name))
        client = make_node_client(node)
        view_name = self.get_metadata_name('-meta-data')
        url = reverse(view_name, args=['latest', 'local-hostname'])
        response = client.get(url)
        self.assertEqual(
            (http.client.OK, node.fqdn),
            (response.status_code,
             response.content.decode(settings.DEFAULT_CHARSET)))
        self.assertIn('text/plain', response['Content-Type'])

    def test_meta_data_instance_id_returns_system_id(self):
        node = factory.make_Node()
        client = make_node_client(node)
        view_name = self.get_metadata_name('-meta-data')
        url = reverse(view_name, args=['latest', 'instance-id'])
        response = client.get(url)
        self.assertEqual(
            (http.client.OK, node.system_id),
            (response.status_code,
             response.content.decode(settings.DEFAULT_CHARSET)))
        self.assertIn('text/plain', response['Content-Type'])

    def test_public_keys_not_listed_for_node_without_public_keys(self):
        view_name = self.get_metadata_name('-meta-data')
        url = reverse(view_name, args=['latest', ''])
        client = make_node_client()
        response = client.get(url)
        self.assertNotIn(
            'public-keys', response.content.decode(
                settings.DEFAULT_CHARSET).split('\n'))

    def test_public_keys_not_listed_for_comm_node_with_ssh_disabled(self):
        user, _ = factory.make_user_with_keys(n_keys=2, username='my-user')
        node = factory.make_Node(
            owner=user, status=NODE_STATUS.COMMISSIONING, enable_ssh=False)
        view_name = self.get_metadata_name('-meta-data')
        url = reverse(view_name, args=['latest', ''])
        client = make_node_client(node=node)
        response = client.get(url)
        self.assertNotIn(
            'public-keys', response.content.decode(
                settings.DEFAULT_CHARSET).split('\n'))

    def test_public_keys_listed_for_comm_node_with_ssh_enabled(self):
        user, _ = factory.make_user_with_keys(n_keys=2, username='my-user')
        node = factory.make_Node(
            owner=user, status=NODE_STATUS.COMMISSIONING, enable_ssh=True)
        view_name = self.get_metadata_name('-meta-data')
        url = reverse(view_name, args=['latest', ''])
        client = make_node_client(node=node)
        response = client.get(url)
        self.assertIn(
            'public-keys', response.content.decode(
                settings.DEFAULT_CHARSET).split('\n'))

    def test_public_keys_listed_for_node_with_public_keys(self):
        user, _ = factory.make_user_with_keys(n_keys=2, username='my-user')
        node = factory.make_Node(owner=user)
        view_name = self.get_metadata_name('-meta-data')
        url = reverse(view_name, args=['latest', ''])
        client = make_node_client(node=node)
        response = client.get(url)
        self.assertIn(
            'public-keys', response.content.decode(
                settings.DEFAULT_CHARSET).split('\n'))

    def test_public_keys_for_node_without_public_keys_returns_empty(self):
        view_name = self.get_metadata_name('-meta-data')
        url = reverse(view_name, args=['latest', 'public-keys'])
        client = make_node_client()
        response = client.get(url)
        self.assertEqual(
            (http.client.OK, b''),
            (response.status_code, response.content))

    def test_public_keys_for_node_returns_list_of_keys(self):
        user, _ = factory.make_user_with_keys(n_keys=2, username='my-user')
        node = factory.make_Node(owner=user)
        view_name = self.get_metadata_name('-meta-data')
        url = reverse(view_name, args=['latest', 'public-keys'])
        client = make_node_client(node=node)
        response = client.get(url)
        self.assertEqual(http.client.OK, response.status_code)
        keys = SSHKey.objects.filter(user=user).values_list('key', flat=True)
        expected_response = '\n'.join(keys)
        self.assertThat(
            response.content.decode(settings.DEFAULT_CHARSET),
            Equals(expected_response))
        self.assertIn('text/plain', response['Content-Type'])

    def test_public_keys_url_with_additional_slashes(self):
        # The metadata service also accepts urls with any number of additional
        # slashes after 'metadata': e.g. http://host/metadata///rest-of-url.
        user, _ = factory.make_user_with_keys(n_keys=2, username='my-user')
        node = factory.make_Node(owner=user)
        view_name = self.get_metadata_name('-meta-data')
        url = reverse(view_name, args=['latest', 'public-keys'])
        # Insert additional slashes.
        url = url.replace('metadata', 'metadata/////')
        client = make_node_client(node=node)
        response = client.get(url)
        keys = SSHKey.objects.filter(user=user).values_list('key', flat=True)
        self.assertThat(
            response.content.decode(settings.DEFAULT_CHARSET),
            Equals('\n'.join(keys)))

    def test_vendor_data_publishes_yaml(self):
        node = factory.make_Node()
        client = make_node_client(node)
        view_name = self.get_metadata_name('-meta-data')
        url = reverse(view_name, args=['latest', 'vendor-data'])
        response = client.get(url)
        self.assertThat(
            response.get("Content-Type"),
            Equals("application/x-yaml; charset=utf-8"))

    def test_vendor_data_node_with_owner_def_user_includes_system_info(self):
        # Test vendor_data includes system_info when the node has an owner
        # and a default_user set.
        user, _ = factory.make_user_with_keys(n_keys=2, username='my-user')
        node = factory.make_Node(owner=user, default_user=user)
        client = make_node_client(node)
        view_name = self.get_metadata_name('-meta-data')
        url = reverse(view_name, args=['latest', 'vendor-data'])
        response = client.get(url)
        content = yaml.safe_load(response.content)
        self.assertThat(response, HasStatusCode(http.client.OK))
        self.assertThat(content, LooksLikeCloudInit)
        self.assertThat(
            yaml.safe_load(content['cloud-init']),
            KeysEqual("system_info"))

    def test_vendor_data_node_without_def_user_includes_no_system_info(self):
        # Test vendor_data includes no system_info when the node has an owner
        # but doesn't have a default_user set.
        user, _ = factory.make_user_with_keys(n_keys=2, username='my-user')
        node = factory.make_Node(owner=user)
        client = make_node_client(node)
        view_name = self.get_metadata_name('-meta-data')
        url = reverse(view_name, args=['latest', 'vendor-data'])
        response = client.get(url)
        content = yaml.safe_load(response.content)
        self.assertThat(response, HasStatusCode(http.client.OK))
        self.assertThat(content, LooksLikeCloudInit)
        self.assertThat(
            yaml.safe_load(content['cloud-init']),
            HasLength(0))

    def test_vendor_data_for_node_without_owner_includes_no_system_info(self):
        view_name = self.get_metadata_name('-meta-data')
        url = reverse(view_name, args=['latest', 'vendor-data'])
        client = make_node_client()
        response = client.get(url)
        content = yaml.safe_load(response.content)
        self.assertThat(response, HasStatusCode(http.client.OK))
        self.assertThat(content, LooksLikeCloudInit)
        self.assertThat(
            yaml.safe_load(content['cloud-init']),
            HasLength(0))

    def test_vendor_data_calls_through_to_get_vendor_data(self):
        # i.e. for further information, see `get_vendor_data`.
        get_vendor_data = self.patch_autospec(api, "get_vendor_data")
        get_vendor_data.return_value = {"foo": factory.make_name("bar")}
        view_name = self.get_metadata_name('-meta-data')
        url = reverse(view_name, args=['latest', 'vendor-data'])
        node = factory.make_Node()
        client = make_node_client(node)
        response = client.get(url)
        content = yaml.safe_load(response.content)
        self.assertThat(response, HasStatusCode(http.client.OK))
        self.assertThat(content, LooksLikeCloudInit)
        self.assertThat(
            yaml.safe_load(content['cloud-init']),
            Equals(get_vendor_data.return_value))
        self.assertThat(
            get_vendor_data, MockCalledOnceWith(node))


class TestMetadataUserData(MAASServerTestCase):
    """Tests for the metadata user-data API endpoint."""

    def test_user_data_view_returns_binary_data(self):
        node = factory.make_Node(status=NODE_STATUS.COMMISSIONING)
        NodeUserData.objects.set_user_data(node, sample_binary_data)
        client = make_node_client(node)
        response = client.get(reverse('metadata-user-data', args=['latest']))
        self.assertEqual('application/octet-stream', response['Content-Type'])
        self.assertIsInstance(response.content, bytes)
        self.assertEqual(
            (http.client.OK, sample_binary_data),
            (response.status_code, response.content))

    def test_poweroff_user_data_returned_if_unexpected_status(self):
        node = factory.make_Node(status=NODE_STATUS.READY)
        NodeUserData.objects.set_user_data(node, sample_binary_data)
        client = make_node_client(node)
        user_data = factory.make_name('user data').encode("ascii")
        self.patch(
            api, 'generate_user_data_for_poweroff').return_value = user_data
        response = client.get(reverse('metadata-user-data', args=['latest']))
        self.assertEqual('application/octet-stream', response['Content-Type'])
        self.assertIsInstance(response.content, bytes)
        self.assertEqual(
            (http.client.OK, user_data),
            (response.status_code, response.content))

    def test_user_data_for_node_without_user_data_returns_not_found(self):
        client = make_node_client(
            factory.make_Node(status=NODE_STATUS.COMMISSIONING))
        response = client.get(reverse('metadata-user-data', args=['latest']))
        self.assertEqual(http.client.NOT_FOUND, response.status_code)


class TestMetadataUserDataStateChanges(MAASServerTestCase):
    """Tests for the metadata user-data API endpoint."""

    def setUp(self):
        super(TestMetadataUserDataStateChanges, self).setUp()
        self.useFixture(SignalsDisabled("power"))

    def test_request_does_not_cause_status_change_if_not_deploying(self):
        status = factory.pick_enum(
            NODE_STATUS, but_not=[NODE_STATUS.DEPLOYING])
        node = factory.make_Node(status=status)
        NodeUserData.objects.set_user_data(node, sample_binary_data)
        client = make_node_client(node)
        response = client.get(reverse('metadata-user-data', args=['latest']))
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(status, reload_object(node).status)

    def test_request_causes_status_change_if_deploying(self):
        node = factory.make_Node(status=NODE_STATUS.DEPLOYING)
        NodeUserData.objects.set_user_data(node, sample_binary_data)
        client = make_node_client(node)
        response = client.get(reverse('metadata-user-data', args=['latest']))
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(NODE_STATUS.DEPLOYED, reload_object(node).status)


class TestCurtinMetadataUserData(
        PreseedRPCMixin, MAASTransactionServerTestCase):
    """Tests for the curtin-metadata user-data API endpoint."""

    def test_curtin_user_data_view_returns_curtin_data(self):
        node = factory.make_Node(interface=True)
        nic = node.get_boot_interface()
        nic.vlan.dhcp_on = True
        nic.vlan.primary_rack = self.rpc_rack_controller
        nic.vlan.save()
        arch, subarch = node.architecture.split('/')
        boot_image = make_rpc_boot_image(purpose='xinstall')
        self.patch(
            preseed_module,
            'get_boot_images_for').return_value = [boot_image]
        client = make_node_client(node)
        response = client.get(
            reverse('curtin-metadata-user-data', args=['latest']))

        self.assertEqual(http.client.OK.value, response.status_code)
        self.assertThat(
            response.content.decode(settings.DEFAULT_CHARSET),
            Contains("PREFIX='curtin'"))


class TestInstallingAPI(MAASServerTestCase):

    def setUp(self):
        super(TestInstallingAPI, self).setUp()
        self.useFixture(SignalsDisabled("power"))

    def test_other_user_than_node_cannot_signal_installation_result(self):
        node = factory.make_Node(status=NODE_STATUS.DEPLOYING)
        client = MAASSensibleOAuthClient(factory.make_User())
        response = call_signal(client)
        self.assertEqual(http.client.FORBIDDEN, response.status_code)
        self.assertEqual(
            NODE_STATUS.DEPLOYING, reload_object(node).status)

    def test_signaling_installation_result_does_not_affect_other_node(self):
        other_node = factory.make_Node(
            status=NODE_STATUS.DEPLOYING, with_empty_script_sets=True)
        node = factory.make_Node(
            status=NODE_STATUS.DEPLOYING, with_empty_script_sets=True)
        client = make_node_client(node)
        response = call_signal(client, status=SIGNAL_STATUS.OK)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(
            NODE_STATUS.DEPLOYING, reload_object(other_node).status)

    def test_signaling_installation_success_leaves_node_deploying(self):
        node = factory.make_Node(
            interface=True, status=NODE_STATUS.DEPLOYING,
            with_empty_script_sets=True)
        client = make_node_client(node=node)
        response = call_signal(client, status=SIGNAL_STATUS.OK)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(NODE_STATUS.DEPLOYING, reload_object(node).status)

    def test_signaling_installation_success_does_not_populate_tags(self):
        populate_tags_for_single_node = self.patch(
            api, "populate_tags_for_single_node")
        node = factory.make_Node(
            interface=True, status=NODE_STATUS.DEPLOYING,
            with_empty_script_sets=True)
        client = make_node_client(node=node)
        response = call_signal(client, status=SIGNAL_STATUS.OK)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(NODE_STATUS.DEPLOYING, reload_object(node).status)
        self.assertThat(populate_tags_for_single_node, MockNotCalled())

    def test_signaling_installation_success_is_idempotent(self):
        node = factory.make_Node(
            status=NODE_STATUS.DEPLOYING, with_empty_script_sets=True)
        client = make_node_client(node=node)
        call_signal(client, status=SIGNAL_STATUS.OK)
        response = call_signal(client, status=SIGNAL_STATUS.OK)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(NODE_STATUS.DEPLOYING, reload_object(node).status)

    def test_signaling_installation_success_does_not_clear_owner(self):
        node = factory.make_Node(
            status=NODE_STATUS.DEPLOYING, owner=factory.make_User(),
            with_empty_script_sets=True)
        client = make_node_client(node=node)
        response = call_signal(client, status=SIGNAL_STATUS.OK)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(node.owner, reload_object(node).owner)

    def test_signaling_installation_failure_makes_node_failed(self):
        node = factory.make_Node(
            status=NODE_STATUS.DEPLOYING, owner=factory.make_User(),
            with_empty_script_sets=True)
        client = make_node_client(node=node)
        response = call_signal(client, status=SIGNAL_STATUS.FAILED)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(
            NODE_STATUS.FAILED_DEPLOYMENT, reload_object(node).status)

    def test_signaling_installation_failure_is_idempotent(self):
        node = factory.make_Node(
            status=NODE_STATUS.DEPLOYING, owner=factory.make_User(),
            with_empty_script_sets=True)
        client = make_node_client(node=node)
        call_signal(client, status=SIGNAL_STATUS.FAILED)
        response = call_signal(client, status=SIGNAL_STATUS.FAILED)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(
            NODE_STATUS.FAILED_DEPLOYMENT, reload_object(node).status)

    def test_signaling_installation_updates_last_ping(self):
        start_time = floor(time.time())
        node = factory.make_Node(
            status=NODE_STATUS.DEPLOYING, owner=factory.make_User(),
            with_empty_script_sets=True)
        client = make_node_client(node=node)
        response = call_signal(client, status=SIGNAL_STATUS.WORKING)
        self.assertThat(response, HasStatusCode(http.client.OK))
        end_time = ceil(time.time())
        script_set = node.current_installation_script_set
        self.assertGreaterEqual(
            ceil(script_set.last_ping.timestamp()), start_time)
        self.assertLessEqual(floor(script_set.last_ping.timestamp()), end_time)

    def test_signaling_installation_with_script_id_sets_script_to_run(self):
        node = factory.make_Node(
            status=NODE_STATUS.DEPLOYING, owner=factory.make_User(),
            with_empty_script_sets=True)
        script_result = (
            node.current_installation_script_set.scriptresult_set.first())
        client = make_node_client(node=node)
        response = call_signal(
            client, status=SIGNAL_STATUS.WORKING,
            script_result_id=script_result.id)
        self.assertThat(response, HasStatusCode(http.client.OK))
        script_result = reload_object(script_result)
        self.assertEquals(SCRIPT_STATUS.RUNNING, script_result.status)

    def test_signaling_installation_with_script_id_ignores_not_pending(self):
        node = factory.make_Node(
            status=NODE_STATUS.DEPLOYING, owner=factory.make_User(),
            with_empty_script_sets=True)
        script_result = (
            node.current_installation_script_set.scriptresult_set.first())
        script_status = factory.pick_choice(
            SCRIPT_STATUS_CHOICES, but_not=[SCRIPT_STATUS.PENDING])
        script_result.status = script_status
        script_result.save()
        client = make_node_client(node=node)
        response = call_signal(
            client, status=SIGNAL_STATUS.WORKING,
            script_result_id=script_result.id)
        self.assertThat(response, HasStatusCode(http.client.OK))
        script_result = reload_object(script_result)
        self.assertEquals(script_status, script_result.status)


class TestMAASScripts(MAASServerTestCase):

    def extract_and_validate_file(
            self, tar, path, start_time, end_time, content):
        member = tar.getmember(path)
        self.assertGreaterEqual(member.mtime, start_time)
        self.assertLessEqual(member.mtime, end_time)
        self.assertEqual(0o755, member.mode)
        self.assertEqual(content, tar.extractfile(path).read())

    def test__returns_all_scripts_when_commissioning(self):
        start_time = floor(time.time())
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)

        response = make_node_client(node=node).get(
            reverse('maas-scripts', args=['latest']))
        self.assertEqual(
            http.client.OK, response.status_code,
            "Unexpected response %d: %s"
            % (response.status_code, response.content))
        self.assertEquals('application/x-tar', response['Content-Type'])
        tar = tarfile.open(mode='r', fileobj=BytesIO(response.content))
        end_time = ceil(time.time())
        # The + 1 is for the index.json file.
        self.assertEquals(
            node.current_commissioning_script_set.scriptresult_set.count() +
            node.current_testing_script_set.scriptresult_set.count() + 1,
            len(tar.getmembers()))

        commissioning_meta_data = []
        for script_result in node.current_commissioning_script_set:
            path = os.path.join('commissioning', script_result.name)
            md_item = {
                'name': script_result.name,
                'path': path,
                'script_result_id': script_result.id,
            }
            if script_result.script is None:
                script = NODE_INFO_SCRIPTS[script_result.name]
                content = script['content']
                md_item['timeout_seconds'] = script['timeout'].seconds
            else:
                content = script_result.script.script.data.encode()
                md_item['script_version_id'] = script_result.script.script.id
                md_item['timeout_seconds'] = (
                    script_result.script.timeout.seconds)
            self.extract_and_validate_file(
                tar, path, start_time, end_time, content)
            commissioning_meta_data.append(md_item)

        testing_meta_data = []
        for script_result in node.current_testing_script_set:
            path = os.path.join('testing', script_result.name)
            self.extract_and_validate_file(
                tar, path, start_time, end_time,
                script_result.script.script.data.encode())
            testing_meta_data.append({
                'name': script_result.name,
                'path': path,
                'script_result_id': script_result.id,
                'script_version_id': script_result.script.script.id,
                'timeout_seconds': script_result.script.timeout.seconds,
            })

        meta_data = json.loads(
            tar.extractfile('index.json').read().decode('utf-8'))
        self.assertDictEqual(
            {'1.0': {
                'commissioning_scripts': sorted(
                    commissioning_meta_data, key=itemgetter(
                        'name', 'script_result_id')),
                'testing_scripts': sorted(
                    testing_meta_data, key=itemgetter(
                        'name', 'script_result_id')),
            }}, meta_data)

    def test__returns_testing_scripts_when_testing(self):
        start_time = floor(time.time())
        node = factory.make_Node(
            status=NODE_STATUS.TESTING, with_empty_script_sets=True)

        response = make_node_client(node=node).get(
            reverse('maas-scripts', args=['latest']))
        self.assertEqual(
            http.client.OK, response.status_code,
            "Unexpected response %d: %s"
            % (response.status_code, response.content))
        self.assertEquals('application/x-tar', response['Content-Type'])
        tar = tarfile.open(mode='r', fileobj=BytesIO(response.content))
        end_time = ceil(time.time())
        # The + 1 is for the index.json file.
        self.assertEquals(
            node.current_testing_script_set.scriptresult_set.count() + 1,
            len(tar.getmembers()))

        testing_meta_data = []
        for script_result in node.current_testing_script_set:
            path = os.path.join('testing', script_result.name)
            self.extract_and_validate_file(
                tar, path, start_time, end_time,
                script_result.script.script.data.encode())
            testing_meta_data.append({
                'name': script_result.name,
                'path': path,
                'script_result_id': script_result.id,
                'script_version_id': script_result.script.script.id,
                'timeout_seconds': script_result.script.timeout.seconds,
            })

        meta_data = json.loads(
            tar.extractfile('index.json').read().decode('utf-8'))
        self.assertDictEqual(
            {'1.0': {
                'testing_scripts': sorted(
                    testing_meta_data, key=itemgetter(
                        'name', 'script_result_id')),
            }}, meta_data)

    def test__removes_scriptless_script_result(self):
        node = factory.make_Node(
            status=NODE_STATUS.TESTING, with_empty_script_sets=True)

        script_result = (
            node.current_testing_script_set.scriptresult_set.first())
        script_result.script.delete()

        response = make_node_client(node=node).get(
            reverse('maas-scripts', args=['latest']))
        self.assertEqual(
            http.client.OK, response.status_code,
            "Unexpected response %d: %s"
            % (response.status_code, response.content))
        self.assertEquals('application/x-tar', response['Content-Type'])
        tar = tarfile.open(mode='r', fileobj=BytesIO(response.content))
        self.assertEquals(1, len(tar.getmembers()))

        self.assertEquals(
            0, node.current_testing_script_set.scriptresult_set.count())

        meta_data = json.loads(
            tar.extractfile('index.json').read().decode('utf-8'))
        self.assertDictEqual({'1.0': {}}, meta_data)

    def test__only_returns_scripts_which_havnt_been_run(self):
        start_time = floor(time.time())
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)

        script = factory.make_Script(script_type=SCRIPT_TYPE.COMMISSIONING)
        already_run_commissioning_script = factory.make_ScriptResult(
            script_set=node.current_commissioning_script_set,
            script=script, status=SCRIPT_STATUS.PASSED)
        script = factory.make_Script(script_type=SCRIPT_TYPE.TESTING)
        already_run_testing_script = factory.make_ScriptResult(
            script_set=node.current_testing_script_set,
            script=script, status=SCRIPT_STATUS.FAILED)

        response = make_node_client(node=node).get(
            reverse('maas-scripts', args=['latest']))
        self.assertEqual(
            http.client.OK, response.status_code,
            "Unexpected response %d: %s"
            % (response.status_code, response.content))
        self.assertEquals('application/x-tar', response['Content-Type'])
        tar = tarfile.open(mode='r', fileobj=BytesIO(response.content))
        end_time = ceil(time.time())
        # We have two scripts which have been run but the tar always includes
        # an index.json file so subtract one.
        self.assertEquals(
            node.current_commissioning_script_set.scriptresult_set.count() +
            node.current_testing_script_set.scriptresult_set.count() - 1,
            len(tar.getmembers()))

        commissioning_meta_data = []
        for script_result in node.current_commissioning_script_set:
            if script_result.id == already_run_commissioning_script.id:
                continue
            path = os.path.join('commissioning', script_result.name)
            md_item = {
                'name': script_result.name,
                'path': path,
                'script_result_id': script_result.id,
            }
            if script_result.script is None:
                script = NODE_INFO_SCRIPTS[script_result.name]
                content = script['content']
                md_item['timeout_seconds'] = script['timeout'].seconds
            else:
                content = script_result.script.script.data.encode()
                md_item['script_version_id'] = script_result.script.script.id
                md_item['timeout_seconds'] = (
                    script_result.script.timeout.seconds)
            self.extract_and_validate_file(
                tar, path, start_time, end_time, content)
            commissioning_meta_data.append(md_item)

        testing_meta_data = []
        for script_result in node.current_testing_script_set:
            if script_result.id == already_run_testing_script.id:
                continue
            path = os.path.join('testing', script_result.name)
            md_item = {
                'name': script_result.name,
                'path': path,
                'script_result_id': script_result.id,
                'script_version_id': script_result.script.script.id,
                'timeout_seconds': script_result.script.timeout.seconds,
            }
            content = script_result.script.script.data.encode()
            self.extract_and_validate_file(
                tar, path, start_time, end_time, content)
            testing_meta_data.append(md_item)

        meta_data = json.loads(
            tar.extractfile('index.json').read().decode('utf-8'))
        self.assertDictEqual(
            {'1.0': {
                'commissioning_scripts': sorted(
                    commissioning_meta_data, key=itemgetter(
                        'name', 'script_result_id')),
                'testing_scripts': sorted(
                    testing_meta_data, key=itemgetter(
                        'name', 'script_result_id')),
            }}, meta_data)


class TestCommissioningAPI(MAASServerTestCase):

    def setUp(self):
        super(TestCommissioningAPI, self).setUp()
        self.useFixture(SignalsDisabled("power"))

    def test_commissioning_scripts(self):
        start_time = floor(time.time())
        # Create custom commissioing scripts
        binary_script = factory.make_Script(
            script_type=SCRIPT_TYPE.COMMISSIONING,
            script=VersionedTextFile.objects.create(
                data=base64.b64encode(sample_binary_data)),
            )
        text_script = factory.make_Script(
            script_type=SCRIPT_TYPE.COMMISSIONING,
            script=VersionedTextFile.objects.create(
                data=factory.make_string()),
            )
        response = make_node_client().get(
            reverse('commissioning-scripts', args=['latest']))
        self.assertEqual(
            http.client.OK, response.status_code,
            "Unexpected response %d: %s"
            % (response.status_code, response.content))
        self.assertIn(
            response['Content-Type'],
            {
                'application/tar',
                'application/x-gtar',
                'application/x-tar',
                'application/x-tgz',
            })
        archive = tarfile.open(fileobj=BytesIO(response.content))
        end_time = ceil(time.time())

        # Validate all builtin scripts are included
        for script in NODE_INFO_SCRIPTS.values():
            path = os.path.join('commissioning.d', script['name'])
            member = archive.getmember(path)
            self.assertGreaterEqual(member.mtime, start_time)
            self.assertLessEqual(member.mtime, end_time)
            self.assertEqual(0o755, member.mode)
            self.assertEqual(
                script['content'], archive.extractfile(path).read())

        # Validate custom binary commissioning script
        path = os.path.join('commissioning.d', binary_script.name)
        member = archive.getmember(path)
        self.assertGreaterEqual(member.mtime, start_time)
        self.assertLessEqual(member.mtime, end_time)
        self.assertEqual(0o755, member.mode)
        self.assertEqual(sample_binary_data, archive.extractfile(path).read())

        # Validate custom text commissioning script
        path = os.path.join('commissioning.d', text_script.name)
        member = archive.getmember(path)
        self.assertGreaterEqual(member.mtime, start_time)
        self.assertLessEqual(member.mtime, end_time)
        self.assertEqual(0o755, member.mode)
        self.assertEqual(
            text_script.script.data,
            archive.extractfile(path).read().decode('utf-8'))

    def test_other_user_than_node_cannot_signal_commissioning_result(self):
        node = factory.make_Node(status=NODE_STATUS.COMMISSIONING)
        client = MAASSensibleOAuthClient(factory.make_User())
        response = call_signal(client)
        self.assertEqual(http.client.FORBIDDEN, response.status_code)
        self.assertEqual(
            NODE_STATUS.COMMISSIONING, reload_object(node).status)

    def test_signaling_commissioning_result_does_not_affect_other_node(self):
        other_node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        client = make_node_client(node)
        response = call_signal(client, status=SIGNAL_STATUS.OK)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(
            NODE_STATUS.COMMISSIONING, reload_object(other_node).status)

    def test_signaling_commissioning_OK_repopulates_tags(self):
        populate_tags_for_single_node = self.patch(
            api, "populate_tags_for_single_node")
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        client = make_node_client(node)
        response = call_signal(
            client, status=SIGNAL_STATUS.OK, script_result=0)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(NODE_STATUS.READY, reload_object(node).status)
        self.assertThat(
            populate_tags_for_single_node,
            MockCalledOnceWith(ANY, node))

    def test_signaling_requires_status_code(self):
        node = factory.make_Node(status=NODE_STATUS.COMMISSIONING)
        client = make_node_client(node=node)
        url = reverse('metadata-version', args=['latest'])
        response = client.post(url, {'op': 'signal'})
        self.assertEqual(http.client.BAD_REQUEST, response.status_code)

    def test_signaling_rejects_unknown_status_code(self):
        response = call_signal(status=factory.make_string())
        self.assertEqual(http.client.BAD_REQUEST, response.status_code)

    def test_signaling_refuses_if_machine_in_unexpected_state(self):
        machine = factory.make_Node(status=NODE_STATUS.NEW)
        client = make_node_client(node=machine)
        response = call_signal(client)
        self.expectThat(
            response.status_code,
            Equals(http.client.CONFLICT))
        self.expectThat(
            response.content.decode(settings.DEFAULT_CHARSET),
            Equals(
                "Machine wasn't commissioning/installing/entering-rescue-mode"
                " (status is New)"))

    def test_signaling_accepts_non_machine_results(self):
        node = factory.make_Node(
            with_empty_script_sets=True,
            node_type=factory.pick_choice(
                NODE_TYPE_CHOICES, but_not=[NODE_TYPE.MACHINE]))
        script_result = (
            node.current_commissioning_script_set.scriptresult_set.first())
        script_result.status = SCRIPT_STATUS.RUNNING
        script_result.save()
        client = make_node_client(node=node)
        exit_status = random.randint(0, 255)
        output = factory.make_string()
        response = call_signal(
            client, script_result=exit_status,
            files={script_result.name: output.encode('ascii')})
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        script_result = reload_object(script_result)
        self.assertEqual(exit_status, script_result.exit_status)
        self.assertEqual(output, script_result.output.decode('utf-8'))

    def test_signaling_accepts_WORKING_status(self):
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        client = make_node_client(node=node)
        response = call_signal(client, status=SIGNAL_STATUS.WORKING)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(
            NODE_STATUS.COMMISSIONING, reload_object(node).status)

    def test_signaling_stores_exit_status(self):
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        script_result = (
            node.current_commissioning_script_set.scriptresult_set.first())
        script_result.status = SCRIPT_STATUS.RUNNING
        script_result.save()
        client = make_node_client(node=node)
        exit_status = random.randint(0, 255)
        response = call_signal(
            client, script_result=exit_status,
            files={script_result.name: factory.make_string().encode('ascii')})
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        script_result = reload_object(script_result)
        self.assertEqual(exit_status, script_result.exit_status)

    def test_signaling_stores_empty_script_result(self):
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        script_result = (
            node.current_commissioning_script_set.scriptresult_set.first())
        script_result.status = SCRIPT_STATUS.RUNNING
        script_result.save()
        client = make_node_client(node=node)
        response = call_signal(
            client, script_result=random.randint(0, 255),
            files={script_result.name: ''.encode('ascii')})
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        script_result = reload_object(script_result)
        self.assertEqual(b'', script_result.stdout)

    def test_signaling_WORKING_keeps_owner(self):
        user = factory.make_User()
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        node.owner = user
        node.save()
        client = make_node_client(node=node)
        response = call_signal(client, status=SIGNAL_STATUS.WORKING)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(user, reload_object(node).owner)

    def test_signaling_commissioning_success_makes_node_ready(self):
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        client = make_node_client(node=node)
        response = call_signal(client, status=SIGNAL_STATUS.OK)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(NODE_STATUS.READY, reload_object(node).status)

    def test_signaling_commissioning_success_is_idempotent(self):
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        client = make_node_client(node=node)
        call_signal(client, status=SIGNAL_STATUS.OK)
        response = call_signal(client, status=SIGNAL_STATUS.OK)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(NODE_STATUS.READY, reload_object(node).status)

    def test_signaling_commissioning_failure_makes_node_failed_tests(self):
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        client = make_node_client(node=node)
        response = call_signal(client, status=SIGNAL_STATUS.FAILED)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(
            NODE_STATUS.FAILED_COMMISSIONING, reload_object(node).status)
        for script_result in node.current_testing_script_set:
            self.assertEqual(SCRIPT_STATUS.ABORTED, script_result.status)

    def test_signaling_commissioning_failure_does_not_populate_tags(self):
        populate_tags_for_single_node = self.patch(
            api, "populate_tags_for_single_node")
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        client = make_node_client(node=node)
        response = call_signal(client, status=SIGNAL_STATUS.FAILED)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertThat(populate_tags_for_single_node, MockNotCalled())

    def test_signaling_commissioning_clears_status_expires(self):
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, status_expires=datetime.now(),
            with_empty_script_sets=True)
        client = make_node_client(node=node)
        response = call_signal(client, status=SIGNAL_STATUS.WORKING)
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        self.assertIsNone(reload_object(node).status_expires)

    def test_signaling_commissioning_failure_is_idempotent(self):
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        client = make_node_client(node=node)
        call_signal(client, status=SIGNAL_STATUS.FAILED)
        response = call_signal(client, status=SIGNAL_STATUS.FAILED)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(
            NODE_STATUS.FAILED_COMMISSIONING, reload_object(node).status)

    def test_signaling_commissioning_failure_sets_node_error(self):
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        client = make_node_client(node=node)
        error_text = factory.make_string()
        response = call_signal(
            client, status=SIGNAL_STATUS.FAILED, error=error_text)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(error_text, reload_object(node).error)

    def test_signaling_no_error_clears_existing_error(self):
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, error=factory.make_string(),
            with_empty_script_sets=True)
        client = make_node_client(node=node)
        response = call_signal(client)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual('', reload_object(node).error)

    def test_signaling_stores_files_for_any_status(self):
        self.useFixture(SignalsDisabled("power"))
        statuses = ['WORKING', 'OK', 'FAILED']
        nodes = {}
        script_results = {}
        for status in statuses:
            node = factory.make_Node(
                status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
            script_result = (
                node.current_commissioning_script_set.scriptresult_set.first())
            script_result.status = SCRIPT_STATUS.RUNNING
            script_result.save()
            nodes[status] = node
            script_results[status] = script_result
        for status, node in nodes.items():
            script_result = script_results[status]
            client = make_node_client(node=node)
            exit_status = random.randint(0, 10)
            call_signal(
                client, status=status,
                script_result=exit_status,
                files={script_result.name: factory.make_bytes()})
        for script_result in script_results.values():
            self.assertIsNotNone(script_result.stdout)

    def test_signal_stores_file_contents(self):
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        script_result = (
            node.current_commissioning_script_set.scriptresult_set.first())
        script_result.status = SCRIPT_STATUS.RUNNING
        script_result.save()
        client = make_node_client(node=node)
        text = factory.make_string().encode('ascii')
        exit_status = random.randint(0, 255)
        response = call_signal(
            client, script_result=exit_status,
            files={script_result.name: text})
        script_result = reload_object(script_result)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(text, script_result.output)

    def test_signal_stores_binary(self):
        unicode_text = '<\u2621>'
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        script_result = (
            node.current_commissioning_script_set.scriptresult_set.first())
        script_result.status = SCRIPT_STATUS.RUNNING
        script_result.save()
        client = make_node_client(node=node)
        exit_status = random.randint(0, 10)
        response = call_signal(
            client, script_result=exit_status,
            files={script_result.name: unicode_text.encode('utf-8')})
        script_result = reload_object(script_result)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(unicode_text.encode("utf-8"), script_result.output)

    def test_signal_stores_multiple_files(self):
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        script_results = []
        contents = {}
        for script_result in node.current_commissioning_script_set:
            script_result.status = SCRIPT_STATUS.RUNNING
            script_result.save()
            script_results.append(script_result)

            contents[script_result.name] = factory.make_string().encode(
                'ascii')

        client = make_node_client(node=node)
        exit_status = random.randint(0, 255)
        response = call_signal(
            client, script_result=exit_status, files=contents)

        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(
            contents,
            {
                script_result.name: reload_object(script_result).output
                for script_result in script_results
            })

    def test_signal_stores_files_up_to_documented_size_limit(self):
        # The documented size limit for commissioning result files:
        # one megabyte.  What happens above this limit is none of
        # anybody's business, but files up to this size should work.
        size_limit = 2 ** 20
        contents = factory.make_string(size_limit, spaces=True)
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        script_result = (
            node.current_commissioning_script_set.scriptresult_set.first())
        script_result.status = SCRIPT_STATUS.RUNNING
        script_result.save()
        client = make_node_client(node=node)
        exit_status = random.randint(0, 255)
        response = call_signal(
            client, script_result=exit_status,
            files={script_result.name: contents.encode('utf-8')})
        script_result = reload_object(script_result)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(size_limit, len(script_result.output))

    def test_signal_stores_timeout(self):
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        script_result = (
            node.current_commissioning_script_set.scriptresult_set.first())
        script_result.status = SCRIPT_STATUS.RUNNING
        script_result.save()
        client = make_node_client(node=node)
        text = factory.make_string().encode('ascii')
        exit_status = random.randint(0, 255)
        response = call_signal(
            client, script_result=exit_status,
            files={script_result.name: text}, status=SIGNAL_STATUS.TIMEDOUT)
        script_result = reload_object(script_result)
        self.assertEqual(http.client.OK, response.status_code)
        script_result = reload_object(script_result)
        self.assertEqual(text, script_result.output)
        self.assertEqual(SCRIPT_STATUS.TIMEDOUT, script_result.status)
        self.assertIsNone(script_result.exit_status)

    def test_signal_stores_virtual_tag_on_node_if_virtual(self):
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        client = make_node_client(node=node)
        content = 'qemu'.encode('utf-8')
        response = call_signal(
            client, script_result=0,
            files={'00-maas-02-virtuality.out': content})
        self.assertEqual(http.client.OK, response.status_code)
        node = reload_object(node)
        self.assertEqual(
            ['virtual'], [each_tag.name for each_tag in node.tags.all()])

    def test_signal_removes_virtual_tag_on_node_if_not_virtual(self):
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        tag, _ = Tag.objects.get_or_create(name='virtual')
        node.tags.add(tag)
        client = make_node_client(node=node)
        content = 'none'.encode('utf-8')
        response = call_signal(
            client, script_result=0,
            files={'00-maas-02-virtuality.out': content})
        self.assertEqual(http.client.OK, response.status_code)
        node = reload_object(node)
        self.assertEqual(
            [], [each_tag.name for each_tag in node.tags.all()])

    def test_signal_leaves_untagged_physical_node_unaltered(self):
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        client = make_node_client(node=node)
        content = 'none'.encode('utf-8')
        response = call_signal(
            client, script_result=0,
            files={'00-maas-02-virtuality.out': content})
        self.assertEqual(http.client.OK, response.status_code)
        node = reload_object(node)
        self.assertEqual(0, len(node.tags.all()))

    def test_signal_current_power_type_mscm_does_not_store_params(self):
        node = factory.make_Node(
            power_type="mscm", status=NODE_STATUS.COMMISSIONING,
            with_empty_script_sets=True)
        client = make_node_client(node=node)
        params = dict(
            power_address=factory.make_ipv4_address(),
            power_user=factory.make_string(),
            power_pass=factory.make_string())
        with SignalsDisabled("power"):
            response = call_signal(
                client, power_type="moonshot",
                power_parameters=json.dumps(params))
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        node = reload_object(node)
        self.assertEqual("mscm", node.power_type)
        self.assertNotEqual(params, node.power_parameters)

    def test_signal_current_power_type_rsd_does_not_store_params(self):
        node = factory.make_Node(
            power_type="rsd", status=NODE_STATUS.COMMISSIONING,
            with_empty_script_sets=True)
        client = make_node_client(node=node)
        params = dict(
            power_address=factory.make_ipv4_address(),
            power_user=factory.make_string(),
            power_pass=factory.make_string())
        with SignalsDisabled("power"):
            response = call_signal(
                client, power_type="rsd",
                power_parameters=json.dumps(params))
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        node = reload_object(node)
        self.assertEqual("rsd", node.power_type)
        self.assertNotEqual(params, node.power_parameters)

    def test_signal_refuses_bad_power_type(self):
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        client = make_node_client(node=node)
        response = call_signal(client, power_type="foo")
        self.expectThat(
            response.status_code,
            Equals(http.client.BAD_REQUEST))
        self.assertThat(
            response.content.decode(settings.DEFAULT_CHARSET),
            Equals("Bad power_type 'foo'"))

    def test_signal_power_type_stores_params(self):
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        client = make_node_client(node=node)
        params = dict(
            power_address=factory.make_ipv4_address(),
            power_user=factory.make_string(),
            power_pass=factory.make_string())
        response = call_signal(
            client, power_type="ipmi", power_parameters=json.dumps(params))
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        node = reload_object(node)
        self.assertEqual("ipmi", node.power_type)
        self.assertEqual(params, node.power_parameters)

    def test_signal_power_type_lower_case_works(self):
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        client = make_node_client(node=node)
        params = dict(
            power_address=factory.make_ipv4_address(),
            power_user=factory.make_string(),
            power_pass=factory.make_string())
        response = call_signal(
            client, power_type="ipmi", power_parameters=json.dumps(params))
        self.assertEqual(
            http.client.OK, response.status_code, response.content)
        node = reload_object(node)
        self.assertEqual(
            params, node.power_parameters)

    def test_signal_invalid_power_parameters(self):
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        client = make_node_client(node=node)
        response = call_signal(
            client, power_type="ipmi", power_parameters="badjson")
        self.expectThat(
            response.status_code,
            Equals(http.client.BAD_REQUEST))
        self.expectThat(
            response.content.decode(settings.DEFAULT_CHARSET),
            Equals("Failed to parse JSON power_parameters"))

    def test_signal_sets_default_storage_layout_if_OK(self):
        self.patch_autospec(Node, "set_default_storage_layout")
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        client = make_node_client(node)
        response = call_signal(
            client, status=SIGNAL_STATUS.OK, script_result=0)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(NODE_STATUS.READY, reload_object(node).status)
        self.assertThat(
            Node.set_default_storage_layout,
            MockCalledOnceWith(node))

    def test_signal_sets_default_storage_layout_if_TESTING(self):
        self.patch_autospec(Node, "set_default_storage_layout")
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        client = make_node_client(node)
        response = call_signal(
            client, status=SIGNAL_STATUS.TESTING, script_result=0)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(NODE_STATUS.TESTING, reload_object(node).status)
        self.assertThat(
            Node.set_default_storage_layout,
            MockCalledOnceWith(node))

    def test_signal_does_not_set_default_storage_layout_if_rack(self):
        self.patch_autospec(Node, "set_default_storage_layout")
        node = factory.make_RackController(with_empty_script_sets=True)
        client = make_node_client(node)
        response = call_signal(
            client, status=SIGNAL_STATUS.OK, script_result=0)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertThat(
            Node.set_default_storage_layout,
            MockNotCalled())

    def test_signal_does_not_set_default_storage_layout_if_WORKING(self):
        self.patch_autospec(Node, "set_default_storage_layout")
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        client = make_node_client(node)
        response = call_signal(
            client, status=SIGNAL_STATUS.WORKING, script_result=0)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertThat(
            Node.set_default_storage_layout,
            MockNotCalled())

    def test_signal_does_not_set_default_storage_layout_if_FAILED(self):
        self.patch_autospec(Node, "set_default_storage_layout")
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        client = make_node_client(node)
        response = call_signal(
            client, status=SIGNAL_STATUS.FAILED, script_result=0)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertThat(
            Node.set_default_storage_layout,
            MockNotCalled())

    def test_signal_calls_sets_initial_network_config_if_OK(self):
        self.useFixture(SignalsDisabled("power"))
        mock_set_initial_networking_configuration = self.patch_autospec(
            Node, "set_initial_networking_configuration")
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        client = make_node_client(node)
        response = call_signal(
            client, status=SIGNAL_STATUS.OK, script_result=0)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(NODE_STATUS.READY, reload_object(node).status)
        self.assertThat(
            mock_set_initial_networking_configuration,
            MockCalledOnceWith(node))

    def test_signal_calls_sets_initial_network_config_if_TESTING(self):
        self.useFixture(SignalsDisabled("power"))
        mock_set_initial_networking_configuration = self.patch_autospec(
            Node, "set_initial_networking_configuration")
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        client = make_node_client(node)
        response = call_signal(
            client, status=SIGNAL_STATUS.TESTING, script_result=0)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(NODE_STATUS.TESTING, reload_object(node).status)
        self.assertThat(
            mock_set_initial_networking_configuration,
            MockCalledOnceWith(node))

    def test_signal_doesnt_call_sets_initial_network_config_if_rack(self):
        mock_set_initial_networking_configuration = self.patch_autospec(
            Node, "set_initial_networking_configuration")
        node = factory.make_RackController(with_empty_script_sets=True)
        client = make_node_client(node)
        response = call_signal(
            client, status=SIGNAL_STATUS.OK, script_result=0)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertThat(
            mock_set_initial_networking_configuration,
            MockNotCalled())

    def test_signal_doesnt_call_sets_initial_network_config_if_WORKING(self):
        mock_set_initial_networking_configuration = self.patch_autospec(
            Node, "set_initial_networking_configuration")
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        client = make_node_client(node)
        response = call_signal(
            client, status=SIGNAL_STATUS.WORKING, script_result=0)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertThat(
            mock_set_initial_networking_configuration,
            MockNotCalled())

    def test_signal_doesnt_call_sets_initial_network_config_if_FAILED(self):
        mock_set_initial_networking_configuration = self.patch_autospec(
            Node, "set_initial_networking_configuration")
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        client = make_node_client(node)
        response = call_signal(
            client, status=SIGNAL_STATUS.FAILED, script_result=0)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertThat(
            mock_set_initial_networking_configuration,
            MockNotCalled())

    def test_signaling_commissioning_updates_last_ping(self):
        start_time = floor(time.time())
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, owner=factory.make_User(),
            with_empty_script_sets=True)
        client = make_node_client(node=node)
        response = call_signal(client, status=SIGNAL_STATUS.WORKING)
        self.assertThat(response, HasStatusCode(http.client.OK))
        end_time = ceil(time.time())
        script_set = node.current_commissioning_script_set
        self.assertGreaterEqual(
            ceil(script_set.last_ping.timestamp()), start_time)
        self.assertLessEqual(floor(script_set.last_ping.timestamp()), end_time)

    def test_signaling_commissioning_with_script_id_sets_script_to_run(self):
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, owner=factory.make_User(),
            with_empty_script_sets=True)
        script_result = (
            node.current_commissioning_script_set.scriptresult_set.first())
        client = make_node_client(node=node)
        response = call_signal(
            client, status=SIGNAL_STATUS.WORKING,
            script_result_id=script_result.id)
        self.assertThat(response, HasStatusCode(http.client.OK))
        script_result = reload_object(script_result)
        self.assertEquals(SCRIPT_STATUS.RUNNING, script_result.status)

    def test_signaling_commissioning_with_script_id_ignores_not_pending(self):
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, owner=factory.make_User(),
            with_empty_script_sets=True)
        script_result = (
            node.current_commissioning_script_set.scriptresult_set.first())
        script_status = factory.pick_choice(
            SCRIPT_STATUS_CHOICES, but_not=[SCRIPT_STATUS.PENDING])
        script_result.status = script_status
        script_result.save()
        client = make_node_client(node=node)
        response = call_signal(
            client, status=SIGNAL_STATUS.WORKING,
            script_result_id=script_result.id)
        self.assertThat(response, HasStatusCode(http.client.OK))
        script_result = reload_object(script_result)
        self.assertEquals(script_status, script_result.status)


class TestTestingAPI(MAASServerTestCase):

    def setUp(self):
        super().setUp()
        self.useFixture(SignalsDisabled("power"))

    def test_other_user_than_node_cannot_signal_testing_result(self):
        node = factory.make_Node(status=NODE_STATUS.TESTING)
        client = MAASSensibleOAuthClient(factory.make_User())
        response = call_signal(client)
        self.assertThat(response, HasStatusCode(http.client.FORBIDDEN))
        self.assertEqual(NODE_STATUS.TESTING, reload_object(node).status)

    def test_signaling_testing_result_does_not_affect_other_node(self):
        other_node = factory.make_Node(
            status=NODE_STATUS.TESTING, with_empty_script_sets=True)
        node = factory.make_Node(
            previous_status=NODE_STATUS.DEPLOYED, status=NODE_STATUS.TESTING,
            with_empty_script_sets=True)
        client = make_node_client(node)
        response = call_signal(client, status=SIGNAL_STATUS.OK)
        self.assertThat(response, HasStatusCode(http.client.OK))
        self.assertEqual(NODE_STATUS.TESTING, reload_object(other_node).status)

    def test_signaling_testing_success_moves_node_to_previous_status(self):
        node = factory.make_Node(
            previous_status=NODE_STATUS.DEPLOYED, status=NODE_STATUS.TESTING,
            with_empty_script_sets=True)
        client = make_node_client(node=node)
        response = call_signal(client, status=SIGNAL_STATUS.OK)
        self.assertThat(response, HasStatusCode(http.client.OK))
        self.assertEqual(NODE_STATUS.DEPLOYED, reload_object(node).status)

    def test_signaling_testing_success_moves_node_to_ready_when_commiss(self):
        node = factory.make_Node(
            previous_status=NODE_STATUS.COMMISSIONING,
            status=NODE_STATUS.TESTING, with_empty_script_sets=True)
        client = make_node_client(node=node)
        response = call_signal(client, status=SIGNAL_STATUS.OK)
        self.assertThat(response, HasStatusCode(http.client.OK))
        self.assertEqual(NODE_STATUS.READY, reload_object(node).status)

    def test_signaling_testing_success_moves_node_to_new_when_f_commiss(self):
        node = factory.make_Node(
            previous_status=NODE_STATUS.FAILED_COMMISSIONING,
            status=NODE_STATUS.TESTING, with_empty_script_sets=True)
        client = make_node_client(node=node)
        response = call_signal(client, status=SIGNAL_STATUS.OK)
        self.assertThat(response, HasStatusCode(http.client.OK))
        self.assertEqual(NODE_STATUS.NEW, reload_object(node).status)

    def test_signaling_testing_success_does_not_clear_owner(self):
        node = factory.make_Node(
            previous_status=NODE_STATUS.DEPLOYED, status=NODE_STATUS.TESTING,
            owner=factory.make_User(), with_empty_script_sets=True)
        client = make_node_client(node=node)
        response = call_signal(client, status=SIGNAL_STATUS.OK)
        self.assertThat(response, HasStatusCode(http.client.OK))
        self.assertEqual(node.owner, reload_object(node).owner)

    def test_signaling_testing_failure_makes_node_failed(self):
        node = factory.make_Node(
            status=NODE_STATUS.TESTING, owner=factory.make_User(),
            with_empty_script_sets=True)
        client = make_node_client(node=node)
        response = call_signal(client, status=SIGNAL_STATUS.FAILED)
        self.assertThat(response, HasStatusCode(http.client.OK))
        self.assertEqual(
            NODE_STATUS.FAILED_TESTING, reload_object(node).status)

    def test_signaling_testing_testing_transitions_to_testing(self):
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, owner=factory.make_User(),
            with_empty_script_sets=True)
        client = make_node_client(node=node)
        response = call_signal(client, status=SIGNAL_STATUS.TESTING)
        self.assertThat(response, HasStatusCode(http.client.OK))
        self.assertEqual(NODE_STATUS.TESTING, reload_object(node).status)

    def test_signaling_testing_updates_last_ping(self):
        start_time = floor(time.time())
        node = factory.make_Node(
            status=NODE_STATUS.TESTING, owner=factory.make_User(),
            with_empty_script_sets=True)
        client = make_node_client(node=node)
        response = call_signal(client, status=SIGNAL_STATUS.WORKING)
        self.assertThat(response, HasStatusCode(http.client.OK))
        end_time = ceil(time.time())
        script_set = node.current_testing_script_set
        self.assertGreaterEqual(
            ceil(script_set.last_ping.timestamp()), start_time)
        self.assertLessEqual(floor(script_set.last_ping.timestamp()), end_time)

    def test_signaling_testing_with_script_id_sets_script_to_run(self):
        node = factory.make_Node(
            status=NODE_STATUS.TESTING, owner=factory.make_User(),
            with_empty_script_sets=True)
        script_result = (
            node.current_testing_script_set.scriptresult_set.first())
        client = make_node_client(node=node)
        response = call_signal(
            client, status=SIGNAL_STATUS.WORKING,
            script_result_id=script_result.id)
        self.assertThat(response, HasStatusCode(http.client.OK))
        script_result = reload_object(script_result)
        self.assertEquals(SCRIPT_STATUS.RUNNING, script_result.status)

    def test_signaling_testing_with_script_id_ignores_not_pending(self):
        node = factory.make_Node(
            status=NODE_STATUS.TESTING, owner=factory.make_User(),
            with_empty_script_sets=True)
        script_result = (
            node.current_testing_script_set.scriptresult_set.first())
        script_status = factory.pick_choice(
            SCRIPT_STATUS_CHOICES, but_not=[SCRIPT_STATUS.PENDING])
        script_result.status = script_status
        script_result.save()
        client = make_node_client(node=node)
        response = call_signal(
            client, status=SIGNAL_STATUS.WORKING,
            script_result_id=script_result.id)
        self.assertThat(response, HasStatusCode(http.client.OK))
        script_result = reload_object(script_result)
        self.assertEquals(script_status, script_result.status)

    def test_signaling_testing_resets_status_expires(self):
        factory.make_Script(script_type=SCRIPT_TYPE.TESTING)
        node = factory.make_Node(
            status=NODE_STATUS.TESTING, owner=factory.make_User(),
            with_empty_script_sets=True)
        node.status_expires = datetime.now()
        node.save()
        script_result = (
            node.current_testing_script_set.scriptresult_set.first())
        client = make_node_client(node=node)
        response = call_signal(
            client, status=SIGNAL_STATUS.WORKING,
            script_result_id=script_result.id)
        self.assertThat(response, HasStatusCode(http.client.OK))
        node = reload_object(node)
        self.assertIsNone(node.status_expires)


class TestDiskErasingAPI(MAASServerTestCase):

    def setUp(self):
        super(TestDiskErasingAPI, self).setUp()
        self.useFixture(SignalsDisabled("power"))

    def test_signaling_erasing_failure_makes_node_failed_erasing(self):
        node = factory.make_Node(
            status=NODE_STATUS.DISK_ERASING, owner=factory.make_User())
        client = make_node_client(node=node)
        response = call_signal(client, status=SIGNAL_STATUS.FAILED)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(
            NODE_STATUS.FAILED_DISK_ERASING, reload_object(node).status)

    def test_signaling_erasing_ok_releases_node(self):
        self.patch(Node, "_stop")
        node = factory.make_Node(
            status=NODE_STATUS.DISK_ERASING, owner=factory.make_User(),
            power_state=POWER_STATE.ON, power_type="virsh")
        client = make_node_client(node=node)
        response = call_signal(client, status=SIGNAL_STATUS.OK)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(
            NODE_STATUS.RELEASING, reload_object(node).status)


class TestRescueModeAPI(MAASServerTestCase):

    def setUp(self):
        super(TestRescueModeAPI, self).setUp()
        self.useFixture(SignalsDisabled("power"))

    def test_signaling_rescue_mode_failure_makes_failed_status(self):
        node = factory.make_Node(
            status=NODE_STATUS.ENTERING_RESCUE_MODE, owner=factory.make_User())
        client = make_node_client(node=node)
        response = call_signal(client, status=SIGNAL_STATUS.FAILED)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(
            NODE_STATUS.FAILED_ENTERING_RESCUE_MODE,
            reload_object(node).status)

    def test_signaling_entering_rescue_mode_ok_changes_status(self):
        node = factory.make_Node(
            status=NODE_STATUS.ENTERING_RESCUE_MODE, owner=factory.make_User(),
            power_state=POWER_STATE.ON, power_type="virsh")
        client = make_node_client(node=node)
        response = call_signal(client, status=SIGNAL_STATUS.OK)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertEqual(
            NODE_STATUS.RESCUE_MODE, reload_object(node).status)

    def test_signaling_entering_rescue_mode_does_not_set_owner_to_None(self):
        node = factory.make_Node(
            status=NODE_STATUS.ENTERING_RESCUE_MODE, owner=factory.make_User())
        client = make_node_client(node=node)
        response = call_signal(client, status=SIGNAL_STATUS.OK)
        self.assertEqual(http.client.OK, response.status_code)
        self.assertIsNotNone(reload_object(node).owner)


class TestByMACMetadataAPI(MAASServerTestCase):

    def test_api_retrieves_node_metadata_by_mac(self):
        node = factory.make_Node_with_Interface_on_Subnet()
        iface = node.get_boot_interface()
        url = reverse(
            'metadata-meta-data-by-mac',
            args=['latest', iface.mac_address, 'instance-id'])
        response = self.client.get(url)
        self.assertEqual(
            (http.client.OK.value,
             iface.node.system_id),
            (response.status_code,
             response.content.decode(settings.DEFAULT_CHARSET)))

    def test_api_retrieves_node_userdata_by_mac(self):
        node = factory.make_Node_with_Interface_on_Subnet(
            status=NODE_STATUS.COMMISSIONING)
        iface = node.get_boot_interface()
        user_data = factory.make_string().encode('ascii')
        NodeUserData.objects.set_user_data(iface.node, user_data)
        url = reverse(
            'metadata-user-data-by-mac', args=['latest', iface.mac_address])
        response = self.client.get(url)
        self.assertEqual(
            (http.client.OK, user_data),
            (response.status_code, response.content))

    def test_api_normally_disallows_anonymous_node_metadata_access(self):
        self.patch(settings, 'ALLOW_UNSAFE_METADATA_ACCESS', False)
        node = factory.make_Node_with_Interface_on_Subnet()
        iface = node.get_boot_interface()
        url = reverse(
            'metadata-meta-data-by-mac',
            args=['latest', iface.mac_address, 'instance-id'])
        response = self.client.get(url)
        self.assertEqual(http.client.FORBIDDEN, response.status_code)


class TestNetbootOperationAPI(MAASServerTestCase):

    def test_netboot_off(self):
        node = factory.make_Node(netboot=True)
        client = make_node_client(node=node)
        url = reverse('metadata-version', args=['latest'])
        response = client.post(url, {'op': 'netboot_off'})
        node = reload_object(node)
        self.assertFalse(node.netboot, response)

    def test_netboot_on(self):
        node = factory.make_Node(netboot=False)
        client = make_node_client(node=node)
        url = reverse('metadata-version', args=['latest'])
        response = client.post(url, {'op': 'netboot_on'})
        node = reload_object(node)
        self.assertTrue(node.netboot, response)


class TestAnonymousAPI(MAASServerTestCase):

    def test_anonymous_netboot_off(self):
        node = factory.make_Node(netboot=True)
        anon_netboot_off_url = reverse(
            'metadata-node-by-id', args=['latest', node.system_id])
        response = self.client.post(
            anon_netboot_off_url, {'op': 'netboot_off'})
        node = reload_object(node)
        self.assertEqual(
            (http.client.OK, False),
            (response.status_code, node.netboot),
            response)

    def test_anonymous_get_enlist_preseed(self):
        # The preseed for enlistment can be obtained anonymously.
        anon_enlist_preseed_url = reverse(
            'metadata-enlist-preseed', args=['latest'])
        # Fake the preseed so we're just exercising the view.
        fake_preseed = factory.make_string()
        self.patch(api, "get_enlist_preseed", Mock(return_value=fake_preseed))
        response = self.client.get(
            anon_enlist_preseed_url, {'op': 'get_enlist_preseed'})
        self.assertEqual(
            (http.client.OK.value,
             "text/plain",
             fake_preseed),
            (response.status_code,
             response["Content-Type"],
             response.content.decode(settings.DEFAULT_CHARSET)),
            response)

    def test_anonymous_get_enlist_preseed_detects_request_origin(self):
        url = 'http://%s' % factory.make_name('host')
        network = IPNetwork("10.1.1/24")
        ip = factory.pick_ip_in_network(network)
        rack = factory.make_RackController(interface=True, url=url)
        nic = rack.get_boot_interface()
        vlan = nic.vlan
        subnet = factory.make_Subnet(cidr=str(network.cidr), vlan=vlan)
        factory.make_StaticIPAddress(subnet=subnet, interface=nic)
        vlan.dhcp_on = True
        vlan.primary_rack = rack
        vlan.save()
        anon_enlist_preseed_url = reverse(
            'metadata-enlist-preseed', args=['latest'])
        response = self.client.get(
            anon_enlist_preseed_url, {'op': 'get_enlist_preseed'},
            REMOTE_ADDR=ip)
        self.assertThat(
            response.content.decode(settings.DEFAULT_CHARSET),
            Contains(url))

    def test_anonymous_get_preseed(self):
        # The preseed for a node can be obtained anonymously.
        node = factory.make_Node()
        anon_node_url = reverse(
            'metadata-node-by-id',
            args=['latest', node.system_id])
        # Fake the preseed so we're just exercising the view.
        fake_preseed = factory.make_string()
        self.patch(api, "get_preseed", lambda node: fake_preseed)
        response = self.client.get(
            anon_node_url, {'op': 'get_preseed'})
        self.assertEqual(
            (http.client.OK.value,
             "text/plain",
             fake_preseed),
            (response.status_code,
             response["Content-Type"],
             response.content.decode(settings.DEFAULT_CHARSET)),
            response)

    def test_anoymous_netboot_off_adds_installation_finished_event(self):
        node = factory.make_Node(netboot=True)
        anon_netboot_off_url = reverse(
            'metadata-node-by-id', args=['latest', node.system_id])
        self.client.post(
            anon_netboot_off_url, {'op': 'netboot_off'})
        latest_event = Event.objects.filter(node=node).last()
        self.assertEqual(
            (
                EVENT_TYPES.NODE_INSTALLATION_FINISHED,
                EVENT_DETAILS[
                    EVENT_TYPES.NODE_INSTALLATION_FINISHED].description,
                "Node disabled netboot",
            ),
            (
                latest_event.type.name,
                latest_event.type.description,
                latest_event.description,
            ))


class TestEnlistViews(MAASServerTestCase):
    """Tests for the enlistment metadata views."""

    def test_get_instance_id(self):
        # instance-id must be available
        md_url = reverse(
            'enlist-metadata-meta-data',
            args=['latest', 'instance-id'])
        response = self.client.get(md_url)
        self.assertEqual(
            (http.client.OK.value, "text/plain"),
            (response.status_code, response["Content-Type"]))
        # just insist content is non-empty. It doesn't matter what it is.
        self.assertTrue(response.content)

    def test_get_hostname(self):
        # instance-id must be available
        md_url = reverse(
            'enlist-metadata-meta-data', args=['latest', 'local-hostname'])
        response = self.client.get(md_url)
        self.assertEqual(
            (http.client.OK, "text/plain"),
            (response.status_code, response["Content-Type"]))
        # just insist content is non-empty. It doesn't matter what it is.
        self.assertTrue(response.content)

    def test_public_keys_returns_empty(self):
        # An enlisting node has no SSH keys, but it does request them.
        # If the node insists, we give it the empty list.
        md_url = reverse(
            'enlist-metadata-meta-data', args=['latest', 'public-keys'])
        response = self.client.get(md_url)
        self.assertEqual(
            (http.client.OK, ""),
            (response.status_code,
             response.content.decode(settings.DEFAULT_CHARSET)))

    def test_metadata_bogus_is_404(self):
        md_url = reverse(
            'enlist-metadata-meta-data',
            args=['latest', 'BOGUS'])
        response = self.client.get(md_url)
        self.assertEqual(http.client.NOT_FOUND, response.status_code)

    def test_get_userdata(self):
        # instance-id must be available
        ud_url = reverse('enlist-metadata-user-data', args=['latest'])
        fake_preseed = factory.make_string()
        self.patch(
            api, "get_enlist_userdata", Mock(return_value=fake_preseed))
        response = self.client.get(ud_url)
        self.assertEqual(
            (http.client.OK, "text/plain", fake_preseed),
            (response.status_code, response["Content-Type"],
             response.content.decode(settings.DEFAULT_CHARSET)),
            response)

    def test_get_userdata_detects_request_origin(self):
        rack_url = 'http://%s' % factory.make_name('host')
        maas_url = factory.make_simple_http_url()
        self.useFixture(RegionConfigurationFixture(maas_url=maas_url))
        network = IPNetwork("10.1.1/24")
        ip = factory.pick_ip_in_network(network)
        rack = factory.make_RackController(interface=True, url=rack_url)
        nic = rack.get_boot_interface()
        vlan = nic.vlan
        subnet = factory.make_Subnet(cidr=str(network.cidr), vlan=vlan)
        factory.make_StaticIPAddress(subnet=subnet, interface=nic)
        vlan.dhcp_on = True
        vlan.primary_rack = rack
        vlan.save()
        url = reverse('enlist-metadata-user-data', args=['latest'])
        response = self.client.get(url, REMOTE_ADDR=ip)
        self.assertThat(
            response.content.decode(settings.DEFAULT_CHARSET),
            MatchesAll(Contains(rack_url), Not(Contains(maas_url))))

    def test_metadata_list(self):
        # /enlist/latest/metadata request should list available keys
        md_url = reverse('enlist-metadata-meta-data', args=['latest', ""])
        response = self.client.get(md_url)
        self.assertEqual(
            (http.client.OK, "text/plain"),
            (response.status_code, response["Content-Type"]))
        self.assertThat(
            response.content.decode(settings.DEFAULT_CHARSET).splitlines(),
            ContainsAll(('instance-id', 'local-hostname')))

    def test_api_version_contents_list(self):
        # top level api (/enlist/latest/) must list 'metadata' and 'userdata'
        md_url = reverse('enlist-version', args=['latest'])
        response = self.client.get(md_url)
        self.assertEqual(
            (http.client.OK, "text/plain"),
            (response.status_code, response["Content-Type"]))
        self.assertThat(
            response.content.decode(settings.DEFAULT_CHARSET).splitlines(),
            ContainsAll(('user-data', 'meta-data')))