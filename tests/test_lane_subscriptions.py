"""Callback migration snapshots real agentctl envelope fields, using synthetic subscriptions."""
from unittest.mock import patch

from coordkit.actl import AgentctlClient
from coordkit.process import ToolError
from tests.support import FakeCase


class LaneSubscriptionTests(FakeCase):
    def test_inventory_preserves_expiry_host_execution_and_event_sink(self):
        row = dict(id='sub-example', origin_host_id='source-host-id', state='expired',
                   filter=dict(execution_ids=['exec-example'], kinds=['terminal', 'attention']),
                   destination=dict(kind='file', path=str(self.manifest.paths.events)),
                   expires_at='2020-01-01T00:00:00Z')
        with patch.object(AgentctlClient, 'call', return_value=dict(subscriptions=[row])):
            entries = AgentctlClient(self.manifest).lane_subscriptions()
        self.assertEqual(entries[0]['execution_id'], 'exec-example')
        self.assertEqual(entries[0]['authority_host'], 'source-host-id')
        self.assertEqual(entries[0]['expires_at'], row['expires_at'])
        self.assertEqual(entries[0]['target'], str(self.manifest.paths.events))
        self.assertEqual(entries[0]['ttl'], '72h')

    def test_unlisted_or_oversized_subscription_response_refuses_incomplete_inventory(self):
        for response in ({}, dict(subscriptions=[{}]*4097)):
            with patch.object(AgentctlClient, 'call', return_value=response), self.assertRaises(ToolError):
                AgentctlClient(self.manifest).lane_subscriptions()

    def test_recreation_uses_destination_sink_and_renews_72h(self):
        with patch.object(AgentctlClient, 'call') as call:
            AgentctlClient(self.manifest).subscribe('exec-example')
        self.assertEqual(call.call_args.args, ('subscribe', 'create', '--execution', 'exec-example',
                         '--destination', 'file', '--target', self.manifest.paths.events,
                         '--kind', 'terminal', '--kind', 'attention', '--ttl', '72h'))

    def test_inventory_preserves_multiple_active_filter_sets_per_execution(self):
        rows = [dict(id='sub-'+kind, origin_host_id='source', state='active',
                     filter=dict(execution_ids=['exec-example'], kinds=[kind]),
                     destination=dict(kind='file', path=str(self.manifest.paths.events)),
                     expires_at='2999-01-01T00:00:00Z') for kind in ('attention', 'terminal')]
        with patch.object(AgentctlClient, 'call', return_value=dict(subscriptions=rows)):
            entries = AgentctlClient(self.manifest).lane_subscriptions()
        self.assertEqual(len(entries), 2)
        self.assertEqual({tuple(e['kinds']) for e in entries}, {('terminal',), ('attention',)})
