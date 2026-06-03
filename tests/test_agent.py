"""Fleet Sync Agent unit tests — parser + report shape.

Runs locally with no network. Validates that parse_game_log produces a
captain report matching the server's allowlist.

Run:
    python3 tests/test_agent.py
"""
import json
import sys
import unittest
from pathlib import Path

# Make sync_agent importable from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sync_agent  # noqa: E402


SAMPLE_LOG = """\
<2026-06-01T07:00:00.000Z> [Init] Process sc-client started: branch=sc-alpha (Env: LIVE)
<2026-06-01T07:00:01.000Z> [CSessionManager::OnClientSpawned] Spawned!
<2026-06-01T07:00:02.000Z> <AttachmentReceived> Player[EmperorKahless] Attachment[Armor_Pilot_Light, helmet_a, 1].entity[1234567] Port[Body_ItemPort]
<2026-06-01T07:00:03.000Z> <RequestLocationInventory> Player[EmperorKahless] requested inventory for Location[Stanton3_Area18]
<2026-06-01T07:00:04.000Z> [VehicleNav] Stanton| AEGS_Avenger_Titan_4242[42]|CSCItemNavigation registered
<2026-06-01T07:00:05.000Z> Player has selected point OOC_Stanton_3_ArcCorp on the starmap
<2026-06-01T07:00:06.000Z> <SHUDEvent_OnNotification> Added notification "Entered UEE Jurisdiction"
<2026-06-01T07:00:07.000Z> <SHUDEvent_OnNotification> Added notification "Entered Monitored Space"
"""


class ParserTests(unittest.TestCase):
    def test_extracts_captain(self):
        r = sync_agent.parse_game_log(SAMPLE_LOG)
        self.assertEqual(r["captain"], "EmperorKahless")

    def test_status_active_after_spawn(self):
        r = sync_agent.parse_game_log(SAMPLE_LOG)
        self.assertEqual(r["status"], "active")

    def test_location_resolved(self):
        r = sync_agent.parse_game_log(SAMPLE_LOG)
        self.assertEqual(r["location"]["code"], "Stanton3_Area18")
        self.assertEqual(r["location"]["label"], "Area18, ArcCorp")

    def test_ship_extracted(self):
        r = sync_agent.parse_game_log(SAMPLE_LOG)
        self.assertEqual(r["ship"]["className"], "AEGS_Avenger_Titan")
        self.assertEqual(r["ship"]["entityId"], "4242")

    def test_route_extracted(self):
        r = sync_agent.parse_game_log(SAMPLE_LOG)
        self.assertEqual(r["route"]["code"], "OOC_Stanton_3_ArcCorp")
        self.assertEqual(r["route"]["label"], "ArcCorp")

    def test_conditions(self):
        r = sync_agent.parse_game_log(SAMPLE_LOG)
        c = r["conditions"]
        self.assertTrue(c["monitored"])
        self.assertEqual(c["jurisdiction"], "UEE")

    def test_offline_after_quit(self):
        log_with_quit = SAMPLE_LOG + '<2026-06-01T07:01:00.000Z> <SystemQuit> reason=user, code=0\n'
        r = sync_agent.parse_game_log(log_with_quit)
        self.assertEqual(r["status"], "offline")
        self.assertIsNone(r.get("ship"))

    def test_returns_none_without_player(self):
        r = sync_agent.parse_game_log("<some>line without player\n")
        self.assertIsNone(r)

    def test_report_round_trips_through_json(self):
        r = sync_agent.parse_game_log(SAMPLE_LOG)
        json.loads(json.dumps(r))  # must be json-serializable

    def test_source_tag(self):
        r = sync_agent.parse_game_log(SAMPLE_LOG)
        self.assertEqual(r["source"], "sync-agent")


class RichParserTests(unittest.TestCase):
    """Phase-5 coverage: rich payload (equipment, recent, session, log freshness)."""

    LOG_WITH_GEAR = """\
<2026-06-01T07:00:00.000Z> <Init> Process sc-client started: branch=sc-alpha (Env: LIVE)
<2026-06-01T07:00:01.500Z> <AttachmentReceived> Player[Pilot] Attachment[Armor_Pilot, helmet_a, 1].entity[1] Port[Armor_Head]
<2026-06-01T07:00:01.600Z> <AttachmentReceived> Player[Pilot] Attachment[wep_pistol, sidearm_a, 1].entity[1] Port[wep_sidearm]
<2026-06-01T07:00:02.000Z> [CSessionManager::OnClientSpawned] Spawned!
<2026-06-01T07:00:03.000Z> <VehicleListQuery> ASOPClient.VehicleListReceived: Retrieved 17 entitlements out of 17 (success=true)
"""

    def test_equipment_dict_populated(self):
        r = sync_agent.parse_game_log(self.LOG_WITH_GEAR)
        self.assertIn("Armor_Head", r["equipment"])
        self.assertEqual(r["equipment"]["Armor_Head"], "helmet_a")
        self.assertIn("wep_sidearm", r["equipment"])

    def test_equipment_respawn_drops_unequipped(self):
        # Two spawns: the first equips helmet+sidearm+backpack; the
        # second only re-emits helmet (player dropped the pistol +
        # backpack). The committed equipment should reflect ONLY the
        # second spawn's attachments — no carryover.
        log = (
            "<2026-06-01T07:00:00.000Z> <Init> Process sc-client started: branch=sc-alpha (Env: LIVE)\n"
            "<2026-06-01T07:00:01.500Z> <AttachmentReceived> Player[Pilot] Attachment[Armor_Pilot, helmet_a, 1].entity[1] Port[Armor_Head]\n"
            "<2026-06-01T07:00:01.600Z> <AttachmentReceived> Player[Pilot] Attachment[wep_pistol, sidearm_a, 1].entity[1] Port[wep_sidearm]\n"
            "<2026-06-01T07:00:01.700Z> <AttachmentReceived> Player[Pilot] Attachment[backpack_a, bp_a, 1].entity[1] Port[backpack]\n"
            "<2026-06-01T07:00:02.000Z> [CSessionManager::OnClientSpawned] Spawned!\n"
            # Player respawns later with only a helmet
            "<2026-06-01T08:00:01.500Z> <AttachmentReceived> Player[Pilot] Attachment[Armor_Pilot, helmet_b, 1].entity[1] Port[Armor_Head]\n"
            "<2026-06-01T08:00:02.000Z> [CSessionManager::OnClientSpawned] Spawned!\n"
        )
        r = sync_agent.parse_game_log(log)
        self.assertEqual(r["equipment"].get("Armor_Head"), "helmet_b")
        self.assertNotIn("wep_sidearm", r["equipment"])
        self.assertNotIn("backpack", r["equipment"])

    def test_equipment_storeitem_clears_port(self):
        # Real shape from Bobby's Game.log 2026-06-02: spawn equips deck
        # crew armor + helmet; player then equips the heavy suit
        # (AttachmentReceived) and finally unequips it via the inventory
        # UI (which writes <StoreItem>). The committed dict at end of
        # parse should have NEITHER the deckcrew items (stored at the
        # swap) nor the heavy suit (stored at the unequip).
        log = (
            "<2026-06-02T20:35:00.402Z> <AttachmentReceived> Player[EmperorKahless] "
            "Attachment[rsi_deckcrew_undersuit_01_01_10_310302731219, "
            "rsi_deckcrew_undersuit_01_01_10, 310302731219] Status[persistent] "
            "Port[Armor_Undersuit]\n"
            "<2026-06-02T20:35:00.402Z> <AttachmentReceived> Player[EmperorKahless] "
            "Attachment[rsi_deckcrew_armor_light_helmet_01_01_10_310302731220, "
            "rsi_deckcrew_armor_light_helmet_01_01_10, 310302731220] Status[persistent] "
            "Port[Armor_Helmet]\n"
            "<2026-06-02T20:35:00.500Z> [CSessionManager::OnClientSpawned] Spawned!\n"
            # Swap: store the deckcrew helmet + undersuit, equip the heavy suit
            "<2026-06-02T20:35:58.092Z> <StoreItem> Request[34] store "
            "'rsi_deckcrew_armor_light_helmet_01_01_10_310302731220' [310302731220] by "
            "'EmperorKahless'\n"
            "<2026-06-02T20:35:58.092Z> <StoreItem> Request[35] store "
            "'rsi_deckcrew_undersuit_01_01_10_310302731219' [310302731219] by "
            "'EmperorKahless'\n"
            "<2026-06-02T20:35:59.364Z> <AttachmentReceived> Player[EmperorKahless] "
            "Attachment[clda_env_armor_heavy_suit_01_01_expo_207335002928, "
            "clda_env_armor_heavy_suit_01_01_expo, 207335002928] Status[persistent] "
            "Port[Armor_Undersuit]\n"
            # Unequip the heavy suit
            "<2026-06-02T20:36:08.702Z> <StoreItem> Request[38] store "
            "'clda_env_armor_heavy_suit_01_01_expo_207335002928' [207335002928] by "
            "'EmperorKahless'\n"
        )
        r = sync_agent.parse_game_log(log)
        eq = r["equipment"]
        self.assertNotIn("Armor_Helmet", eq, f"deckcrew helmet should be unequipped, got {eq}")
        self.assertNotIn("Armor_Undersuit", eq, f"heavy suit should be unequipped, got {eq}")

    def test_equipment_post_spawn_pickups_kept(self):
        # Player spawns with helmet, then mid-game picks up a sidearm
        # AFTER the spawn marker. Both should appear in the final dict.
        log = (
            "<2026-06-01T07:00:00.000Z> <Init> Process sc-client started: branch=sc-alpha (Env: LIVE)\n"
            "<2026-06-01T07:00:01.500Z> <AttachmentReceived> Player[Pilot] Attachment[Armor_Pilot, helmet_a, 1].entity[1] Port[Armor_Head]\n"
            "<2026-06-01T07:00:02.000Z> [CSessionManager::OnClientSpawned] Spawned!\n"
            "<2026-06-01T07:30:00.000Z> <AttachmentReceived> Player[Pilot] Attachment[wep_pistol, sidearm_a, 1].entity[1] Port[wep_sidearm]\n"
        )
        r = sync_agent.parse_game_log(log)
        self.assertEqual(r["equipment"].get("Armor_Head"), "helmet_a")
        self.assertEqual(r["equipment"].get("wep_sidearm"), "sidearm_a")

    def test_recent_events_chronological(self):
        r = sync_agent.parse_game_log(self.LOG_WITH_GEAR)
        # `recent` is reversed (newest first)
        self.assertEqual(r["recent"][0]["type"], "ASOP")
        types = [e["type"] for e in r["recent"]]
        self.assertIn("SESSION", types)

    def test_entitlements_extracted(self):
        r = sync_agent.parse_game_log(self.LOG_WITH_GEAR)
        self.assertEqual(r["entitlements"], {"retrieved": 17, "total": 17})

    def test_session_block(self):
        r = sync_agent.parse_game_log(self.LOG_WITH_GEAR)
        self.assertEqual(r["session"]["player"], "Pilot")
        self.assertEqual(r["session"]["environment"], "LIVE")
        self.assertTrue(r["session"]["spawned"])
        self.assertIsNotNone(r["session"]["startedAt"])

    def test_log_block_when_path_provided(self):
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "Game.log"
            p.write_text(self.LOG_WITH_GEAR, encoding="utf-8")
            r = sync_agent.parse_game_log(p.read_text(), p)
            self.assertTrue(r["log"]["exists"])
            self.assertGreater(r["log"]["bytes"], 0)
            self.assertIsNotNone(r["log"]["modifiedAt"])

    def test_refinery_notice(self):
        log = SAMPLE_LOG + '<2026-06-01T07:01:00.000Z> <SHUDEvent_OnNotification> Added notification "Refinery Work Order Completed"\n'
        r = sync_agent.parse_game_log(log)
        self.assertEqual(r["refineryNotice"], "Refinery Work Order Completed")


class ConfigTests(unittest.TestCase):
    def test_default_config_shape(self):
        cfg = sync_agent.load_config()
        self.assertIn("api", cfg)
        self.assertIn("cipher", cfg)
        self.assertIn("live_dir", cfg)
        self.assertIn("enabled", cfg)


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
