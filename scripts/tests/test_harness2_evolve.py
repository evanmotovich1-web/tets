"""Unit and integration tests for the Harness2 self-evolving algorithmic harness."""
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import harness2_evolve as h2e
import options_short_engine as ose
import shadow


class Harness2EvolveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_harness_tree_validates_all_eight_components_and_codex_projection(self):
        h2e.ensure_harness2_importable()
        from harness2.core import tree as h2_tree
        from harness2.substrate.opencode import OpencodeSubstrate

        opencode_dir = self.root / 'opencode_harness'
        h2e.write_harness_tree(opencode_dir, h2e.COMPOSED_POLICY, stage_note='test_v2')
        surface = OpencodeSubstrate().surface()
        read_back = h2_tree.read_tree(surface, opencode_dir)
        stats = h2_tree.validate(surface, read_back, allow_subagents=True)
        self.assertEqual(stats['n_files'], 13)
        self.assertGreater(stats['always_on_chars'], 1000)
        for required_slot in (
            'systemprompt.md',
            'guardrails.md',
            'LongTermMEMORY.md',
            'tool_descriptions/tool_guidance.md',
            'skills/liquidity_cap_gate/SKILL.md',
            'skills/strategy_evolution/SKILL.md',
            'skills/options_and_short_selling/SKILL.md',
            'sub_agents/risk_auditor/AGENT.md',
            'scripts/algo_tools.py',
            'scripts/options_short_engine.py',
            'scripts/run_algo_agent.py',
            'scripts/harness_policy.py',
            'plugin/execution_gate.ts',
        ):
            self.assertIn(required_slot, read_back)

        codex_dir = self.root / 'codex_surface'
        h2e.render_codex_surface_projection(opencode_dir, codex_dir)
        self.assertTrue((codex_dir / 'AGENTS.md').is_file())
        self.assertTrue((codex_dir / '.agents/skills/liquidity_cap_gate/SKILL.md').is_file())
        self.assertTrue((codex_dir / '.agents/skills/strategy_evolution/SKILL.md').is_file())
        self.assertTrue((codex_dir / '.agents/skills/options_and_short_selling/SKILL.md').is_file())
        self.assertTrue((codex_dir / 'sub_agents/risk_auditor/AGENT.md').is_file())
        self.assertTrue((codex_dir / 'scripts/algo_tools.py').is_file())
        self.assertTrue((codex_dir / 'scripts/options_short_engine.py').is_file())
        self.assertTrue((codex_dir / '.codex/hooks.json').is_file())

    def test_options_and_short_engine_supports_all_six_instrument_modes(self):
        _, rows = h2e.load_real_or_fallback_intraday_dataset('SPY')
        profile = h2e.load_operator_profile()
        spot = Decimal(str(rows[-1]['close']))

        occ_c = ose.occ_option_symbol('SPY', '2026-11-01', 'call', Decimal('580.00'))
        occ_p = ose.occ_option_symbol('SPY', '2026-11-01', 'put', Decimal('580.00'))
        self.assertEqual(occ_c, 'SPY261101C00580000')
        self.assertEqual(occ_p, 'SPY261101P00580000')

        q_call = ose.black_scholes_quote(Decimal('580.00'), Decimal('580.00'), 5.0 / 252.0, 0.16, 'call')
        q_put = ose.black_scholes_quote(Decimal('580.00'), Decimal('580.00'), 5.0 / 252.0, 0.16, 'put')
        self.assertGreater(q_call['delta'], 0.45)
        self.assertLess(q_put['delta'], -0.45)
        self.assertGreater(q_call['gamma'], 0)
        self.assertLess(q_call['theta_daily'], 0)

        for mode in ose.INSTRUMENT_MODES:
            spec = {
                'name': f'test_{mode}',
                'instrument_mode': mode,
                'signal_family': 'breakout' if mode in ('long_equity', 'long_call') else 'momentum' if mode in ('short_equity', 'long_put') else 'mean_reversion',
                'symbol': 'SPY',
                'timeframe': '1m',
                'parameters': (
                    {'entry_channel_bars': 135, 'exit_channel_bars': 45, 'min_volume_ratio': '2.1', 'trend_filter_sma': 90}
                    if mode in ('long_equity', 'long_call')
                    else {'lookback_bars': 30, 'enter_return_bps': '35', 'exit_return_bps': '-5', 'min_volume_ratio': '1.8', 'trend_filter_sma': 60}
                    if mode in ('short_equity', 'long_put')
                    else {'lookback_bars': 45, 'enter_z': '-2.2', 'exit_z': '0.4'}
                ),
            }
            sim = ose.simulate_multi_asset(spec, rows, profile)
            self.assertEqual(sim['instrument_mode'], mode)
            self.assertFalse(sim['live_orders'])
            self.assertFalse(sim['broker_orders'])

            order = ose.build_paper_order_preview(spec, spot, profile, rows)
            self.assertEqual(order['instrument_mode'], mode)
            self.assertFalse(order['live_orders'])
            if mode in ('long_equity', 'short_equity'):
                self.assertEqual(order['asset_class'], 'us_equity')
            else:
                self.assertEqual(order['asset_class'], 'us_option')
                self.assertIn('delta', order['greeks'])

    def test_recursive_evolution_improves_over_baseline_and_exports_shadow_ready_strategy(self):
        out_dir = self.root / 'h2_state'
        result = h2e.run_evolution(
            output_dir=out_dir,
            symbol='SPY',
            k=1,
            mode='parallel',
            backend='local',
            tasks_filter=['shadow_pre_tomorrow_session', 'evolve_tomorrow_shadow_catalog'],
        )
        self.assertFalse(result['live_orders'])
        self.assertFalse(result['broker_orders'])
        self.assertFalse(result['promoting'])

        summaries = result['judged_summary']['summaries']
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]['n'], 2)
        self.assertEqual(summaries[0]['score'], 100.0)
        self.assertEqual(summaries[0]['all_pass'], 100.0)
        self.assertEqual(summaries[0]['kept_base'], 0)

        # Verify Experience Library entries were distilled and indexed
        lib_index = out_dir / 'library/k1/agg/algo_strategy_evolution/INDEX.md'
        self.assertTrue(lib_index.is_file())
        self.assertIn('shadow', lib_index.read_text().lower())

        # Verify evolved strategy vs baseline metrics
        package = result['tomorrow_shadow_package']
        cmp_metrics = package['backtest_vs_baseline']
        self.assertGreater(
            Decimal(cmp_metrics['evolved_final_equity']),
            Decimal(cmp_metrics['baseline_final_equity']),
        )
        self.assertGreater(
            Decimal(cmp_metrics['evolved_excess_vs_buy_and_hold_pct']),
            Decimal('0'),
        )
        self.assertLess(
            Decimal(cmp_metrics['evolved_slippage_cost_total']),
            Decimal(cmp_metrics['baseline_slippage_cost_total']),
        )
        self.assertEqual(cmp_metrics['shadow_liquidity_violations'], 0)
        self.assertEqual(cmp_metrics['shadow_budget_violations'], 0)

        # Verify multi-asset book, options chain snapshot, and paper account orders for all 6 modes
        book_path = Path(package['shadow_ready_multi_asset_book_path'])
        chain_path = Path(package['options_chain_snapshot_path'])
        orders_path = Path(package['paper_account_ready_orders_path'])
        self.assertTrue(book_path.is_file())
        self.assertTrue(chain_path.is_file())
        self.assertTrue(orders_path.is_file())

        book_data = json.loads(book_path.read_text())
        orders_data = json.loads(orders_path.read_text())
        for mode in ('long_equity', 'short_equity', 'long_call', 'long_put', 'short_call', 'short_put'):
            self.assertIn(mode, book_data['modes'])
            self.assertIn(mode, orders_data['orders_by_mode'])
            self.assertGreater(Decimal(book_data['modes'][mode]['excess_return_vs_buy_and_hold_pct']), Decimal('0'))

        # Verify exported strategy loads cleanly into shadow.Shadow and executes read-only
        spec_path = Path(package['shadow_ready_strategy_path'])
        self.assertTrue(spec_path.is_file())
        spec = shadow.load_spec(spec_path, symbol='SPY')
        self.assertEqual(spec['schema_version'], 2)
        self.assertEqual(spec['position']['sizing'], 'fixed_notional')
        self.assertLessEqual(Decimal(spec['position']['notional_usd']), Decimal('1000'))

        started = datetime(2026, 10, 26, 13, 30, tzinfo=timezone.utc)
        engine = shadow.Shadow(spec, started)
        snap = engine.snapshot(started)
        self.assertFalse(snap['live_orders'])
        self.assertFalse(snap['broker_orders'])
        self.assertFalse(snap['promoting'])

    def test_agentic_adw_pipeline_synced_triad_and_visual_cli(self) -> None:
        profile = h2e.load_operator_profile()
        self.assertEqual(profile['daily_loss_stop_pct'], '10.0')
        _, rows = h2e.load_real_or_fallback_intraday_dataset(symbol='SPY')
        h_dict = h2e.build_harness_dict(
            h2e.COMPOSED_POLICY, stage_note='composed_evolved_v2'
        )
        adw_run = ose.run_adw_pipeline(
            rows,
            profile,
            gate_policy=h2e.COMPOSED_POLICY['shadow_gate_policy'],
            system_prompt_text=h_dict['systemprompt.md'],
            tool_guidance_text=h_dict['tool_descriptions/tool_guidance.md'],
        )
        self.assertTrue(adw_run['all_phases_passed'])
        self.assertTrue(adw_run['synced_triad_contract']['synced_triad_verified'])
        self.assertEqual(adw_run['synced_triad_contract']['bound_in_prompt_count'], 11)
        self.assertEqual(adw_run['synced_triad_contract']['executed_in_sequence_count'], 11)
        self.assertEqual(len(adw_run['agentic_trader_roster']), 6)
        self.assertEqual(adw_run['shadow_fleet']['fleet_size'], 12)
        self.assertEqual(adw_run['shadow_fleet']['daily_loss_stop_pct'], '10.0')
        self.assertFalse(adw_run['live_orders'])

        cli_text = ose.render_adw_visual_cli(adw_run)
        self.assertIn('SYNCED TRIAD: VERIFIED (11/11 TOOLS)', cli_text.upper())
        self.assertIn('codex:gpt-5.4', cli_text)
        self.assertIn('openrouter:kimi-k2.7-code', cli_text)
        self.assertIn('anthropic/claude: blocked', cli_text.lower())
        self.assertNotIn('claude-sonnet', cli_text.lower())
        for agent_entry in adw_run['agentic_trader_roster']:
            self.assertNotIn('claude', agent_entry['primary_model'].lower())
            self.assertNotIn('anthropic', agent_entry['primary_model'].lower())
        self.assertIn('acct_01_1d_long_equity', cli_text)
        self.assertIn('acct_12_1w_short_put', cli_text)
        for line in cli_text.splitlines():
            self.assertEqual(len(line), 116, f'Line width mismatch ({len(line)} != 116): {line}')

        # Verify Anthropic/Claude models are strictly blocked and custom provider/model sticks
        with self.assertRaises(ValueError):
            ose.resolve_provider_model('claude:claude-sonnet-4-6')
        with self.assertRaises(ValueError):
            ose.resolve_provider_model('anthropic', 'claude-3-7-sonnet')

        with tempfile.TemporaryDirectory(prefix='adw-roster-') as tmp_roster_dir:
            roster_file = Path(tmp_roster_dir) / 'model_roster.json'
            cfg_res = ose.configure_sticky_models(
                state_path=roster_file,
                provider='gemini',
                model='gemini-2.5-pro',
                agent_id='trader_theta_mean_reversion',
            )
            self.assertTrue(cfg_res['persisted'])
            self.assertEqual(
                cfg_res['overrides']['trader_theta_mean_reversion']['primary_model'],
                'gemini:gemini-2.5-pro',
            )

            adw_sticky = ose.run_adw_pipeline(
                rows,
                profile,
                gate_policy=h2e.COMPOSED_POLICY['shadow_gate_policy'],
                system_prompt_text=h_dict['systemprompt.md'],
                tool_guidance_text=h_dict['tool_descriptions/tool_guidance.md'],
                roster_state_path=roster_file,
            )
            theta_agent = next(
                a for a in adw_sticky['agentic_trader_roster']
                if a['agent_id'] == 'trader_theta_mean_reversion'
            )
            self.assertEqual(theta_agent['primary_model'], 'gemini:gemini-2.5-pro')

            # Verify numbered agent selection (e.g. agent '4') + Grok / DeepSeek / GLM / OpenAI providers
            self.assertEqual(ose.resolve_provider_model('grok', 'grok-4'), 'grok:grok-4')
            self.assertEqual(ose.resolve_provider_model('deepseek', 'deepseek-r1'), 'deepseek:deepseek-r1')
            self.assertEqual(ose.resolve_provider_model('glm', 'glm-5'), 'glm:glm-5')
            self.assertEqual(ose.resolve_provider_model('openai', 'gpt-5.4'), 'openai:gpt-5.4')

            ose.configure_sticky_models(
                state_path=roster_file,
                provider='grok',
                model='grok-4',
                agent_id='4',
            )
            reloaded = ose.load_sticky_model_roster(roster_file)
            self.assertEqual(reloaded[3]['primary_model'], 'grok:grok-4')

            keys_file = Path(tmp_roster_dir) / 'provider_keys.json'
            key_res = ose.save_provider_connection('grok', 'xai-demo-token-9876', keys_path=keys_file)
            self.assertTrue(key_res['persisted'])
            self.assertEqual(key_res['masked_status'], 'xai-••••9876')
            menu_text = ose.render_model_and_keys_cli(reloaded, key_res['connections'])
            self.assertIn('Grok (xAI) Subscription & API', menu_text)
            self.assertIn('DeepSeek API', menu_text)
            self.assertIn('GLM (Zhipu AI) API', menu_text)
            self.assertIn('OpenRouter API', menu_text)
            for mline in menu_text.splitlines():
                self.assertEqual(len(mline), 116)


if __name__ == '__main__':
    unittest.main()
