"""SIMULATED tests for egg hatching. They do not log in, open a game socket,
or prove Windows or in-game behavior.
"""
from __future__ import annotations

import asyncio
import os
import unittest
from pathlib import Path

from src.game.eggs.engine import EggEngine, QueryView
from src.game.eggs.presenter import can_start, confirmation_text, format_board
from src.game.eggs.protocol import Limits, format_recv, public_error
from src.game.eggs.store import HoldStore, JsonHoldStore
from src.game.eggs.transport import ContextTransport, Exchange
from src.network.context import MessageSubscription


ROOT = Path(__file__).resolve().parents[1]


class VirtualClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float, should_stop) -> bool:
        if should_stop():
            return False
        self.t += max(0.0, seconds)
        await asyncio.sleep(0)
        return not should_stop()


class ScriptedTransport:
    """In-memory server. Messages queued before a send cannot satisfy that send."""

    def __init__(self, clock: VirtualClock) -> None:
        self.clock = clock
        self.sent: list[tuple] = []
        self.connected = True
        self.should_stop = None
        self.on_send = None
        self._seq = 0
        self._inbox: list[tuple[int, dict]] = []

    def open(self) -> None:
        return None

    def close(self) -> None:
        return None

    def push(self, message: dict) -> None:
        self._seq += 1
        self._inbox.append((self._seq, message))

    def _drop_queued(self) -> None:
        watermark = self._seq
        self._inbox = [item for item in self._inbox if item[0] > watermark]

    async def exchange(self, ext_id, cmd, param, match, timeout):
        if self.should_stop and self.should_stop():
            return Exchange("stopped", sent=False)
        if not self.connected:
            return Exchange("disconnected", sent=False)
        self._drop_queued()
        self.sent.append((ext_id, cmd, dict(param)))
        if self.on_send is not None:
            self.on_send(ext_id, cmd, param)
        if not self.connected:
            return Exchange("disconnected", sent=True)
        return await self._read(match, timeout, sent=True)

    async def wait(self, match, timeout):
        return await self._read(match, timeout, sent=False)

    async def _read(self, match, timeout, *, sent: bool):
        deadline = self.clock.now() + max(0.0, timeout)
        while True:
            for index, (_seq, message) in enumerate(self._inbox):
                if match(message):
                    del self._inbox[index]
                    return Exchange("message", message=message, sent=sent)
            if self.should_stop and self.should_stop():
                return Exchange("stopped", sent=sent)
            if not self.connected:
                return Exchange("disconnected", sent=sent)
            if self.clock.now() >= deadline:
                return Exchange("timeout", sent=sent)
            remaining = deadline - self.clock.now()
            await self.clock.sleep(min(0.05, remaining), lambda: False)


def _limits(**overrides) -> Limits:
    base = dict(
        query_timeout=2.0,
        echo_timeout=5.0,
        collect_timeout=0.8,
        message_box_timeout=0.6,
        poll_interval=1.0,
        readiness_limit=60.0,
    )
    base.update(overrides)
    return Limits(**base)


class World:
    def __init__(self) -> None:
        self.unhatched = [10]
        self.pending: dict[int, int] = {}
        self.st: dict[int, int] = {}
        self.ack = True
        self.ack_id = None
        self.hatch_reply = "echo"
        self.malformed = False
        self.drop_status = False
        self.extra_on_unhatched: dict | None = None

    def status_message(self) -> dict:
        if self.malformed:
            return {"_cmd": "1-8", "es": {"not": "a list"}}
        rows = [
            {"ei": egg_id, "pmid": pmid, "st": self.st.get(egg_id, 0)}
            for egg_id, pmid in self.pending.items()
        ]
        return {"_cmd": "1-8", "es": rows}

    def bind(self, transport: ScriptedTransport, engine: EggEngine) -> None:
        def on_send(_ext, cmd, param):
            if cmd == "1-8":
                if not self.drop_status:
                    transport.push(self.status_message())
                return
            if cmd == "1=1":
                transport.push(
                    {"_cmd": "1=1", "es": [{"ei": egg_id} for egg_id in self.unhatched]}
                )
                if self.extra_on_unhatched is not None:
                    transport.push(self.extra_on_unhatched)
                return
            if cmd == "1-4":
                egg_id = param["eggId"]
                slot = param["id"]
                if self.hatch_reply == "echo":
                    transport.push({"_cmd": "1-4", "eggId": egg_id, "id": slot})
                    self.pending[egg_id] = 8000 + egg_id
                    self.st.setdefault(egg_id, 100)
                elif self.hatch_reply == "box":
                    transport.push({"_cmd": "MessageBox_2", "s": -1, "id": 14})
                elif self.hatch_reply == "unrelated-box":
                    transport.push({"_cmd": "MessageBox_2", "s": -1, "id": 99})
                elif self.hatch_reply == "disconnect":
                    transport.connected = False
                elif self.hatch_reply == "stop":
                    engine.request_stop()
                if egg_id in self.unhatched:
                    self.unhatched.remove(egg_id)
                return
            if cmd == "1223":
                pmid = param["id"]
                if self.ack_id is not None:
                    transport.push({"_cmd": "2223", "id": self.ack_id})
                    return
                if not self.ack:
                    return
                transport.push({"_cmd": "2223", "id": pmid})
                for egg_id, value in list(self.pending.items()):
                    if value == pmid:
                        self.pending.pop(egg_id)
                        self.st.pop(egg_id, None)

        transport.on_send = on_send


def _engine(world: World, **limit_overrides):
    clock = VirtualClock()
    transport = ScriptedTransport(clock)
    engine = EggEngine(
        transport,
        clock=clock,
        store=HoldStore(),
        limits=_limits(**limit_overrides),
        log=lambda _message: None,
    )
    world.bind(transport, engine)
    return engine, transport


def _cmds(transport: ScriptedTransport) -> list[str]:
    return [cmd for _ext, cmd, _param in transport.sent]


class SimulatedEggTests(unittest.IsolatedAsyncioTestCase):
    async def test_simulated_confirmation_sends_nothing(self) -> None:
        engine, transport = _engine(World())
        report = await engine.run(confirmed=False)
        self.assertEqual(transport.sent, [])
        self.assertEqual(report.kind, "confirmation")

    async def test_simulated_loading_store_does_not_send(self) -> None:
        clock = VirtualClock()
        transport = ScriptedTransport(clock)
        store = HoldStore()
        store.upsert(_hold(10, slot=1, phase="hatch_uncertain", hatch_sent=True))
        EggEngine(transport, clock=clock, store=store, limits=_limits())
        self.assertEqual(transport.sent, [])

    async def test_simulated_timeout_is_not_an_empty_list(self) -> None:
        world = World()
        world.drop_status = True
        engine, transport = _engine(world)
        report = await engine.run(confirmed=True)
        self.assertEqual(_cmds(transport), ["1-8"])
        self.assertEqual(report.kind, "timeout")
        self.assertIn("不能当成空列表", report.detail)
        self.assertIsNone((await engine.query()).pending)

    async def test_simulated_malformed_status_sends_no_hatch(self) -> None:
        world = World()
        world.malformed = True
        engine, transport = _engine(world)
        report = await engine.run(confirmed=True)
        self.assertEqual(_cmds(transport), ["1-8"])
        self.assertEqual(report.kind, "malformed")
        self.assertNotIn("1-4", _cmds(transport))

    async def test_simulated_real_empty_list_does_not_hatch(self) -> None:
        world = World()
        world.unhatched = []
        engine, transport = _engine(world)
        report = await engine.run(confirmed=True)
        self.assertEqual(_cmds(transport), ["1-8", "1=1"])
        self.assertEqual(report.kind, "idle")
        self.assertTrue(report.ok)

    async def test_simulated_stale_status_is_ignored(self) -> None:
        world = World()
        world.pending = {5: 50}
        world.st[5] = 100
        world.unhatched = []
        engine, transport = _engine(world)
        transport.push({"_cmd": "1-8", "es": []})
        view = await engine.query()
        self.assertTrue(view.ok)
        self.assertEqual([egg.egg_id for egg in view.pending or []], [5])

    async def test_simulated_stale_collect_ack_is_not_success(self) -> None:
        world = World()
        world.pending = {5: 50}
        world.st[5] = 100
        world.unhatched = []
        world.ack = False
        world.extra_on_unhatched = {"_cmd": "2223", "id": 50}
        engine, transport = _engine(world)
        report = await engine.run(confirmed=True)
        self.assertEqual(_cmds(transport), ["1-8", "1=1", "1223"])
        self.assertNotIn(5, report.collected_ids)
        self.assertTrue(engine.store.collect_blocked(50))
        again, transport2 = _engine(world)
        again.store = engine.store
        second = await again.run(confirmed=True)
        self.assertNotIn("1223", _cmds(transport2))
        self.assertEqual(second.kind, "held")

    async def test_simulated_unrelated_ack_does_not_collect_or_retry(self) -> None:
        world = World()
        world.pending = {5: 50}
        world.st[5] = 100
        world.unhatched = []
        world.ack_id = 999
        engine, transport = _engine(world)
        report = await engine.run(confirmed=True)
        self.assertEqual(report.collected_ids, [])
        self.assertEqual(_cmds(transport).count("1223"), 1)
        self.assertNotIn("1-4", _cmds(transport))

    async def test_simulated_short_wait_does_not_mean_ready(self) -> None:
        world = World()
        world.st_default = 0
        engine, transport = _engine(world, readiness_limit=60)
        seen = {"hatch": False}

        base = transport.on_send

        def wrapped(ext, cmd, param):
            if cmd == "1-4":
                seen["hatch"] = True
            base(ext, cmd, param)
            if cmd == "1-8" and seen["hatch"]:
                engine.request_stop()

        transport.on_send = wrapped
        world.st[10] = 0
        report = await engine.run(confirmed=True)
        self.assertNotIn("1223", _cmds(transport))
        self.assertLess(engine.clock.now(), 1.1)
        self.assertTrue(report.stopped)
        self.assertEqual(engine.store.get(10)["slot"], 1)

    async def test_simulated_readiness_timeout_does_not_reuse_slot(self) -> None:
        world = World()
        world.unhatched = [10, 11]
        engine, transport = _engine(world, readiness_limit=1.0)
        world.bind(transport, engine)
        fresh_handler = transport.on_send

        def dispatch(ext, cmd, param):
            if cmd == "1-4":
                egg_id = param["eggId"]
                transport.push({"_cmd": "1-4", "eggId": egg_id, "id": param["id"]})
                world.pending[egg_id] = 8000 + egg_id
                world.st[egg_id] = 0 if egg_id == 10 else 100
                if egg_id in world.unhatched:
                    world.unhatched.remove(egg_id)
                return
            fresh_handler(ext, cmd, param)

        transport.on_send = dispatch
        await engine.run(confirmed=True)
        hatches = [param for _ext, cmd, param in transport.sent if cmd == "1-4"]
        self.assertEqual(hatches, [{"eggId": 10, "id": 1}, {"eggId": 11, "id": 2}])
        collect_ids = [param["id"] for _ext, cmd, param in transport.sent if cmd == "1223"]
        self.assertEqual(collect_ids, [8011])
        self.assertEqual(engine.store.get(10)["phase"], "readiness_timeout")
        self.assertEqual(engine.store.get(10)["slot"], 1)

    async def test_simulated_stop_blocks_later_sends(self) -> None:
        world = World()
        world.unhatched = [10, 11]
        world.hatch_reply = "stop"
        engine, transport = _engine(world)
        report = await engine.run(confirmed=True)
        self.assertEqual(_cmds(transport).count("1-4"), 1)
        self.assertNotIn("1223", _cmds(transport))
        self.assertTrue(report.stopped)
        self.assertEqual(engine.store.get(10)["slot"], 1)

    async def test_simulated_disconnect_keeps_submitted_slot(self) -> None:
        world = World()
        world.hatch_reply = "disconnect"
        engine, transport = _engine(world)
        report = await engine.run(confirmed=True)
        self.assertEqual(report.kind, "disconnected")
        self.assertEqual(_cmds(transport), ["1-8", "1=1", "1-4"])
        self.assertEqual(engine.store.get(10)["phase"], "hatch_uncertain")
        self.assertEqual(engine.store.get(10)["slot"], 1)

    async def test_simulated_invalid_box_does_not_free_slot_for_reuse(self) -> None:
        world = World()
        world.unhatched = [10, 11]
        engine, transport = _engine(world)
        world.bind(transport, engine)
        base = transport.on_send

        def dispatch(ext, cmd, param):
            if cmd == "1-4" and param["eggId"] == 10:
                transport.push({"_cmd": "MessageBox_2", "s": -1, "id": 14})
                world.unhatched.remove(10)
                return
            base(ext, cmd, param)

        transport.on_send = dispatch
        await engine.run(confirmed=True)
        hatches = [param for _ext, cmd, param in transport.sent if cmd == "1-4"]
        self.assertEqual([item["id"] for item in hatches], [1, 2])
        self.assertEqual(engine.store.get(10)["slot"], 1)
        self.assertNotIn(10, [param["eggId"] for param in hatches[1:]])

    async def test_simulated_collect_still_listed_is_not_retried(self) -> None:
        world = World()
        world.pending = {5: 50}
        world.st[5] = 100
        world.unhatched = []
        world.ack = True
        engine, transport = _engine(world)
        base = transport.on_send

        def dispatch(ext, cmd, param):
            if cmd == "1223":
                transport.push({"_cmd": "2223", "id": param["id"]})
                return
            base(ext, cmd, param)

        transport.on_send = dispatch
        # Keep the egg in the following 1-8 by not removing it.
        report = await engine.run(confirmed=True)
        self.assertEqual(_cmds(transport).count("1223"), 1)
        self.assertEqual(report.collected_ids, [])
        self.assertTrue(engine.store.collect_blocked(50))

    async def test_simulated_happy_path_collects_once_then_frees_slot(self) -> None:
        engine, transport = _engine(World())
        report = await engine.run(confirmed=True)
        self.assertEqual(report.collected_ids, [10])
        self.assertIsNone(engine.store.get(10))
        self.assertEqual(_cmds(transport).count("1223"), 1)
        self.assertEqual(_cmds(transport).count("1-4"), 1)
        self.assertEqual(transport.sent[2][2], {"eggId": 10, "id": 1})
        self.assertEqual(transport.sent[4][2], {"loc": 4, "id": 8010})

    async def test_simulated_unmapped_server_egg_blocks_new_hatch_until_collected(self) -> None:
        world = World()
        world.pending = {4: 40}
        world.st[4] = 100
        world.unhatched = [10]
        engine, transport = _engine(world)
        await engine.run(confirmed=True)
        self.assertEqual(_cmds(transport)[:4], ["1-8", "1=1", "1223", "1-8"])
        self.assertIn("1-4", _cmds(transport))
        self.assertEqual(engine.collected_ids, [4, 10])

    async def test_simulated_unmapped_not_ready_does_not_hatch(self) -> None:
        world = World()
        world.pending = {4: 40}
        world.st[4] = 0
        world.unhatched = [10]
        engine, transport = _engine(world, readiness_limit=1.0)
        report = await engine.run(confirmed=True)
        self.assertNotIn("1-4", _cmds(transport))
        self.assertNotIn("1223", _cmds(transport))
        self.assertEqual(report.kind, "held")

    async def test_simulated_restart_does_not_resend_uncertain_collect(self) -> None:
        store = HoldStore()
        store.upsert(
            _hold(5, slot=None, pmid=50, st=100, phase="collect_uncertain", collect_blocked=True)
        )
        world = World()
        world.pending = {5: 50}
        world.st[5] = 100
        world.unhatched = [10]
        clock = VirtualClock()
        transport = ScriptedTransport(clock)
        engine = EggEngine(transport, clock=clock, store=store, limits=_limits(), log=lambda _m: None)
        world.bind(transport, engine)
        self.assertEqual(transport.sent, [])
        await engine.run(confirmed=True)
        self.assertNotIn("1223", _cmds(transport))
        self.assertNotIn("1-4", _cmds(transport))

    async def test_simulated_explicit_collect_retry_sends_once(self) -> None:
        store = HoldStore()
        store.upsert(
            _hold(5, slot=None, pmid=50, st=100, phase="collect_uncertain", collect_blocked=True, hatch_sent=True)
        )
        world = World()
        world.pending = {5: 50}
        world.st[5] = 100
        world.unhatched = []
        clock = VirtualClock()
        transport = ScriptedTransport(clock)
        engine = EggEngine(transport, clock=clock, store=store, limits=_limits(), log=lambda _m: None)
        world.bind(transport, engine)
        report = await engine.run(confirmed=True, retry_uncertain_collect=True)
        self.assertEqual(report.collected_ids, [5])
        self.assertEqual(_cmds(transport).count("1223"), 1)

    async def test_simulated_disconnect_before_send_undoes_inflight(self) -> None:
        world = World()
        world.pending = {5: 50}
        world.st[5] = 100
        world.unhatched = []
        engine, transport = _engine(world)
        transport.connected = False
        report = await engine.run(confirmed=True)
        self.assertEqual(transport.sent, [])
        self.assertEqual(report.kind, "disconnected")
        self.assertFalse(engine.store.collect_blocked(50))

    def test_simulated_corrupt_store_blocks_start(self) -> None:
        path = ROOT / "logs" / "simulated-egg-state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{", encoding="utf-8")
        store = JsonHoldStore(path)
        self.assertFalse(store.ok)
        self.assertNotIn("{", store.error)

    def test_simulated_logs_omit_secret_fields(self) -> None:
        text = format_recv(
            {
                "_cmd": "1-8",
                "es": [],
                "password": "simulated-password-value",
                "session_id": "simulated-session-value",
            }
        )
        self.assertNotIn("simulated-password-value", text)
        self.assertNotIn("simulated-session-value", text)
        self.assertNotIn("1.1", (ROOT / "src/game/eggs/engine.py").read_text(encoding="utf-8"))
        self.assertNotIn("1.1", (ROOT / "scripts/auto_hatch.py").read_text(encoding="utf-8"))

    def test_simulated_confirmation_copy_and_gate(self) -> None:
        store = HoldStore()
        query = QueryView(True, "ok", "ok", [], [1])
        self.assertFalse(can_start(False, query, True, False))
        self.assertFalse(can_start(True, QueryView(False, "timeout", "超时", None, None), True, False))
        self.assertTrue(can_start(True, query, True, False))
        text = confirmation_text(query, store, False)
        self.assertIn("停止", text)
        self.assertIn("不会", text)
        self.assertIn("还没有查询", format_board(None, store))

    def test_simulated_public_error_redacts_assignment(self) -> None:
        text = public_error(RuntimeError("password=simulated-password-value"))
        self.assertNotIn("simulated-password-value", text)


def _hold(egg_id, *, slot, phase, pmid=None, st=None, hatch_sent=False, collect_blocked=False):
    return {
        "egg_id": egg_id,
        "slot": slot,
        "pmid": pmid,
        "st": st,
        "phase": phase,
        "hatch_sent": hatch_sent,
        "collect_blocked": collect_blocked,
    }


class SimulatedTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_simulated_context_transport_drops_stale_and_unrelated(self) -> None:
        context = _FakeContext()
        transport = ContextTransport(context, lambda: False, lambda _message: None)
        transport.open()
        context.messages.sub._queue.put_nowait({"_cmd": "1-8", "es": []})

        async def send(ext, cmd, param, **_kwargs):
            context.socket.sent.append((ext, cmd, dict(param)))
            await context.messages.sub._queue.put({"_cmd": "2223", "id": 1})
            await context.messages.sub._queue.put(
                {"_cmd": "1-8", "es": [{"ei": 5, "pmid": 9, "st": 100}]}
            )

        context.socket.send_xt_message = send
        result = await transport.exchange(17, "1-8", {}, lambda message: message.get("_cmd") == "1-8", 1)
        self.assertEqual(result.kind, "message")
        self.assertEqual(result.message["es"][0]["ei"], 5)
        transport.close()

    async def test_simulated_context_transport_stop_and_disconnect_do_not_send(self) -> None:
        context = _FakeContext()
        stopped = {"value": True}
        transport = ContextTransport(context, lambda: stopped["value"], lambda _m: None)
        result = await transport.exchange(17, "1-8", {}, lambda _m: True, 0.05)
        self.assertEqual(result.kind, "stopped")
        self.assertEqual(context.socket.sent, [])
        stopped["value"] = False
        context.socket.connected = False
        result = await transport.exchange(17, "1-8", {}, lambda _m: True, 0.05)
        self.assertEqual(result.kind, "disconnected")
        self.assertFalse(result.sent)

    async def test_simulated_subscription_drain(self) -> None:
        subscription = MessageSubscription(_DummyRouter(), asyncio.Queue())
        subscription._queue.put_nowait({"_cmd": "1-8"})
        self.assertEqual(subscription.drain_pending(), 1)
        self.assertEqual(subscription.drain_pending(), 0)
        subscription.close()


class _DummyRouter:
    def unsubscribe(self, _queue) -> None:
        return None


class _FakeMessages:
    def __init__(self) -> None:
        self.disconnected = asyncio.Event()
        self.sub = None

    def subscribe(self, max_queue_size=100):
        self.sub = _QueueSub()
        return self.sub


class _QueueSub:
    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    def drain_pending(self) -> int:
        dropped = 0
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return dropped
            dropped += 1

    async def wait_for(self, predicate, timeout=None):
        async def receive():
            while True:
                message = await self._queue.get()
                if predicate(message):
                    return message

        return await asyncio.wait_for(receive(), timeout)

    def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self) -> None:
        self.messages = _FakeMessages()
        self.socket = type("Socket", (), {"connected": True, "sent": []})()

    def assert_automation_allowed(self) -> None:
        return None


class SimulatedLoginBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_simulated_system_login_skips_config_and_diagnostics(self) -> None:
        from src.accounts import login as login_module

        called = {"config": 0, "save": 0}

        def no_config():
            called["config"] += 1
            raise AssertionError("read config")

        def save(*_args, **_kwargs):
            called["save"] += 1
            return "saved"

        def boom(**_kwargs):
            raise ValueError("simulated-login-failure")

        original_config = login_module.load_user_config
        original_save = login_module._save_login_failure
        original_session = login_module.aiohttp.ClientSession
        login_module.load_user_config = no_config
        login_module._save_login_failure = save
        login_module.aiohttp.ClientSession = boom
        try:
            result = await login_module.http_login(
                "simulated-account",
                "simulated-password-value",
                char_id=1,
                network_mode="system",
                save_diagnostics=False,
            )
        finally:
            login_module.load_user_config = original_config
            login_module._save_login_failure = original_save
            login_module.aiohttp.ClientSession = original_session
        self.assertFalse(result.success)
        self.assertEqual(called["config"], 0)
        self.assertEqual(called["save"], 0)
        self.assertNotIn("simulated-password-value", result.error_msg)

    async def test_simulated_session_uses_direct_connection(self) -> None:
        from src.accounts.login import LoginResult, ZoneData
        from src.game.eggs import session as session_module

        captured = {}
        zone = ZoneData(3, "测试区", "example.invalid", 1, flash_port=1)
        login = LoginResult(
            success=True,
            user_id="user",
            duoduo_id="simulated-account",
            session_id="simulated-session-value",
            zone_list=[zone],
        )

        async def fake_http(account, password, **kwargs):
            captured["http"] = kwargs
            captured["account"] = account
            captured["password"] = password
            return login

        async def fake_connect(zone_arg, user_id, session_id, **kwargs):
            captured["connect"] = kwargs
            captured["zone"] = zone_arg.zone_index
            self.assertNotIn("proxy_url", kwargs)
            return type("Socket", (), {"connected": True})()

        class FakeRouter:
            def __init__(self, socket):
                self.disconnected = asyncio.Event()
                self.socket = socket

            def start(self):
                return None

        class FakeContext:
            def __init__(self, config, result, zone_arg, socket, router):
                captured["config"] = dict(config)
                self.socket = socket
                self.messages = router

            async def wait_for_player_initialization(self):
                return None

            async def close(self):
                return None

        originals = (
            session_module.http_login,
            session_module.login_and_connect,
            session_module.MessageRouter,
            session_module.AppContext,
            session_module.start_player_initialization,
        )
        session_module.http_login = fake_http
        session_module.login_and_connect = fake_connect
        session_module.MessageRouter = FakeRouter
        session_module.AppContext = FakeContext
        session_module.start_player_initialization = lambda *args, **kwargs: None
        logs = []
        os.environ["AOLA_PROXY_URL"] = "socks5://127.0.0.1:9"
        try:
            egg = session_module.EggSession()
            zones = await egg.authenticate(
                "simulated-account", "simulated-password-value", 1, logs.append
            )
            await egg.connect(zones[0].zone_index, logs.append)
        finally:
            os.environ.pop("AOLA_PROXY_URL", None)
            (
                session_module.http_login,
                session_module.login_and_connect,
                session_module.MessageRouter,
                session_module.AppContext,
                session_module.start_player_initialization,
            ) = originals
        self.assertEqual(captured["http"]["network_mode"], "system")
        self.assertFalse(captured["http"]["save_diagnostics"])
        self.assertNotIn("proxy_url", captured["http"])
        self.assertEqual(captured["connect"]["network_mode"], "system")
        self.assertNotIn("tcp_retry_window", captured["connect"])
        self.assertIsInstance(captured["connect"].get("stage_notes"), list)
        self.assertNotIn("password", captured["config"])
        self.assertNotIn("session_id", captured["config"])
        joined = "\n".join(logs)
        self.assertNotIn("simulated-password-value", joined)
        self.assertNotIn("simulated-session-value", joined)
        self.assertIn("直接连接", joined)

    def test_simulated_system_socket_ignores_proxy_env(self) -> None:
        from src.network.proxy import process_proxy
        from src.network import proxy as proxy_module
        from src.network.socket_client import GameSocket

        def boom():
            raise AssertionError("proxy lookup")

        original = proxy_module.process_proxy
        proxy_module.process_proxy = boom
        os.environ["AOLA_PROXY_URL"] = "socks5://127.0.0.1:9"
        try:
            socket = GameSocket("simulated-session-value", network_mode="system")
        finally:
            os.environ.pop("AOLA_PROXY_URL", None)
            proxy_module.process_proxy = original
        self.assertIsNone(socket.proxy_url)
        self.assertIs(process_proxy, proxy_module.process_proxy)


class SimulatedWindowTests(unittest.TestCase):
    def test_simulated_window_starts_idle(self) -> None:
        try:
            import tkinter as tk
            root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"window toolkit unavailable: {exc}")
        root.withdraw()
        from src.game.eggs.ui import EggApp

        store = ROOT / "logs" / "simulated-ui-state.json"
        log = ROOT / "logs" / "simulated-ui.log"
        app = EggApp(root, store_path=store, log_path=log)
        try:
            self.assertEqual(str(app.start_button["state"]), "disabled")
            self.assertFalse(app.retry_collect.get())
            self.assertIn("空闲", app.status.get())
            self.assertEqual(str(app.auth_button["state"]), "disabled")
            self.assertEqual(str(app.connect_button["state"]), "disabled")
            board = app.board.get("1.0", "end")
            self.assertIn("还没有查询", board)
        finally:
            app._on_close()


class SimulatedRoleAndConnectTests(unittest.IsolatedAsyncioTestCase):
    def test_simulated_choose_role_zero_one_and_many(self) -> None:
        from src.accounts.login import RoleInfo
        from src.game.eggs.session import choose_role

        one = RoleInfo(4, "user-a", "小甲")
        two = RoleInfo(9, "user-b", "小乙")
        with self.assertRaises(ValueError):
            choose_role([], None)
        self.assertEqual(choose_role([one], None).char_id, 4)
        with self.assertRaises(ValueError):
            choose_role([one, two], None)
        self.assertEqual(choose_role([one, two], 9).nickname, "小乙")
        with self.assertRaises(ValueError):
            choose_role([one, two], 1)

    def test_simulated_zone_list_uses_login_metadata_only(self) -> None:
        from src.accounts.login import LoginResult, ZoneData
        from src.game.eggs.session import format_zone_choice, list_zone_choices

        result = LoginResult(
            success=True,
            zone_list=[
                ZoneData(3, "普伦星", "203.0.113.10", 443, flash_port=8001, domain="h5.example"),
                ZoneData(8, "无名", "", 0, flash_port=0, domain=""),
            ],
        )
        choices = list_zone_choices(result)
        self.assertEqual([item.zone_index for item in choices], [3])
        self.assertTrue(choices[0].has_tcp)
        self.assertTrue(choices[0].has_h5)
        self.assertEqual(format_zone_choice(choices[0]), "3 普伦星（TCP/H5）")

    def test_simulated_socket_packet_carries_user_id_not_char_id(self) -> None:
        from src.protocol.message_codec import build_login_message

        packet = build_login_message("3 普伦星", "user-field", "sid-field")
        self.assertIn("3 普伦星".encode(), packet)
        self.assertIn(b"user-field", packet)
        self.assertIn(b"sid-field", packet)
        self.assertNotIn(b"charId", packet)

    async def test_simulated_role_query_sends_account_only(self) -> None:
        from src.accounts import login as login_module

        captured = {}

        class Response:
            async def read(self):
                return "7,user-7,星仔|8,user-8,星妹".encode()

            def raise_for_status(self):
                return None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class Session:
            def __init__(self, **_kwargs):
                return None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def post(self, url, data, headers, timeout):
                captured["url"] = url
                captured["data"] = data
                return Response()

        original = login_module.aiohttp.ClientSession
        login_module.aiohttp.ClientSession = Session
        try:
            roles = await login_module.query_roles("simulated-account", network_mode="system")
        finally:
            login_module.aiohttp.ClientSession = original
        self.assertEqual([(role.char_id, role.nickname) for role in roles], [(7, "星仔"), (8, "星妹")])
        self.assertIn("query_role_info", captured["data"])
        self.assertIn("duoduoId", captured["data"])
        self.assertNotIn("password", captured["data"])
        self.assertNotIn("charId", captured["data"])

    async def test_simulated_role_query_failure_and_empty(self) -> None:
        from src.accounts import login as login_module

        class Boom:
            def __init__(self, **_kwargs):
                return None

            async def __aenter__(self):
                raise login_module.aiohttp.ClientError("down")

            async def __aexit__(self, *_args):
                return False

        original = login_module.aiohttp.ClientSession
        login_module.aiohttp.ClientSession = Boom
        try:
            with self.assertRaises(ConnectionError) as raised:
                await login_module.query_roles("simulated-account", network_mode="system")
        finally:
            login_module.aiohttp.ClientSession = original
        self.assertIn("角色查询请求失败", str(raised.exception))

        class EmptyResponse:
            async def read(self):
                return b""

            def raise_for_status(self):
                return None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class EmptySession(Boom):
            async def __aenter__(self):
                return self

            def post(self, *_args, **_kwargs):
                return EmptyResponse()

        login_module.aiohttp.ClientSession = EmptySession
        try:
            with self.assertRaises(ConnectionError) as raised:
                await login_module.query_roles("simulated-account", network_mode="system")
        finally:
            login_module.aiohttp.ClientSession = original
        self.assertIn("没有可用角色", str(raised.exception))

    async def test_simulated_password_failure_does_not_connect(self) -> None:
        from src.accounts.login import LoginResult, RoleInfo
        from src.game.eggs import session as session_module

        calls = []

        async def fake_query(account, **_kwargs):
            calls.append(("query", account))
            return [RoleInfo(1, "user-1", "星仔")]

        async def fake_http(*_args, **_kwargs):
            calls.append("auth")
            return LoginResult(success=False, error_msg="用户名或密码错误！")

        async def fake_connect(*_args, **_kwargs):
            calls.append("socket")
            return None

        originals = (session_module.query_roles, session_module.http_login, session_module.login_and_connect)
        session_module.query_roles = fake_query
        session_module.http_login = fake_http
        session_module.login_and_connect = fake_connect
        logs = []
        try:
            egg = session_module.EggSession()
            roles = await egg.lookup_roles("simulated-account", logs.append)
            self.assertEqual(roles[0].char_id, 1)
            with self.assertRaises(ConnectionError) as raised:
                await egg.authenticate("simulated-account", "simulated-password-value", 1, logs.append)
        finally:
            session_module.query_roles, session_module.http_login, session_module.login_and_connect = originals
        self.assertIn("账号密码验证失败", str(raised.exception))
        self.assertNotIn("区服长连接", str(raised.exception))
        self.assertEqual(calls, [("query", "simulated-account"), "auth"])
        self.assertNotIn("simulated-password-value", "\n".join(logs))

    async def test_simulated_cancel_stops_before_password_and_socket(self) -> None:
        from src.accounts.login import RoleInfo
        from src.game.eggs.session import CancelledStep
        from src.game.eggs import session as session_module

        calls = []

        async def fake_http(*_args, **_kwargs):
            calls.append("auth")
            raise AssertionError("password request")

        async def fake_connect(*_args, **_kwargs):
            calls.append("socket")
            raise AssertionError("socket")

        originals = (session_module.http_login, session_module.login_and_connect)
        session_module.http_login = fake_http
        session_module.login_and_connect = fake_connect
        try:
            egg = session_module.EggSession()
            egg._roles = [RoleInfo(1, "user-1", "星仔")]
            egg.request_cancel()
            with self.assertRaises(CancelledStep):
                await egg.authenticate("simulated-account", "pw", 1, lambda _m: None)
            with self.assertRaises(CancelledStep):
                await egg.connect(3, lambda _m: None)
        finally:
            session_module.http_login, session_module.login_and_connect = originals
        self.assertEqual(calls, [])

    async def test_simulated_tcp_failure_then_h5_and_tcp_only_skip(self) -> None:
        from src.accounts.login import ZoneData
        from src.network import socket_client

        calls = []

        class FakeSocket:
            def __init__(self, *_args, **_kwargs):
                self.disconnect_reason = ""

            async def connect_tcp(self, host, port, timeout=15.0, policy_timeout=None):
                calls.append(("tcp", host, port))
                self.disconnect_reason = "TCP 安全策略等待超过 0.2 秒"
                return False

            async def connect_ws(self, domain, port, timeout=2):
                calls.append(("ws", domain, port))
                self.disconnect_reason = ""
                return True

            async def login(self, zone, user_id, session_id):
                calls.append(("login", zone, user_id, session_id))
                return {"_cmd": "logOK", "id": 7}

            async def close(self):
                calls.append("close")

        zone = ZoneData(3, "普伦星", "203.0.113.10", 443, flash_port=8001, domain="h5.example")
        original = socket_client.GameSocket
        socket_client.GameSocket = FakeSocket
        try:
            notes = []
            sock = await socket_client.login_and_connect(
                zone, "user-field", "sid-field", network_mode="system", stage_notes=notes
            )
            self.assertIsNotNone(sock)
            self.assertEqual(calls[0], ("tcp", "203.0.113.10", 8001))
            self.assertEqual(calls[1][0], "ws")
            self.assertEqual(calls[2][1], "3 普伦星")
            self.assertEqual(calls[2][2], "user-field")
            self.assertIn("Flash TCP 未连上", "；".join(notes))
            calls.clear()
            skipped = []
            sock = await socket_client.login_and_connect(
                zone,
                "user-field",
                "sid-field",
                network_mode="system",
                tcp_retry_window=0.01,
                stage_notes=skipped,
            )
        finally:
            socket_client.GameSocket = original
        self.assertIsNone(sock)
        self.assertNotIn("ws", [item[0] for item in calls if isinstance(item, tuple)])
        self.assertIn("已跳过 H5 WebSocket", "；".join(skipped))

    async def test_simulated_connect_reports_stage_when_socket_returns_none(self) -> None:
        from src.accounts.login import LoginResult, ZoneData
        from src.game.eggs import session as session_module

        async def fake_connect(*_args, **kwargs):
            kwargs["stage_notes"].append("Flash TCP 未连上：TCP 安全策略等待超过 0.2 秒")
            kwargs["stage_notes"].append("H5 WebSocket 未连上：WSS 连接失败")
            return None

        original = session_module.login_and_connect
        session_module.login_and_connect = fake_connect
        logs = []
        try:
            egg = session_module.EggSession()
            egg._login = LoginResult(
                success=True,
                user_id="user-field",
                session_id="sid-field",
                zone_list=[ZoneData(3, "普伦星", "203.0.113.10", 443, flash_port=8001, domain="h5.example")],
            )
            with self.assertRaises(ConnectionError) as raised:
                await egg.connect(3, logs.append)
        finally:
            session_module.login_and_connect = original
        message = str(raised.exception)
        self.assertIn("区服长连接登录失败", message)
        self.assertIn("Flash TCP 未连上", message)
        self.assertIn("H5 WebSocket 未连上", message)
        self.assertNotIn("sid-field", message)
        self.assertNotIn("user-field", "\n".join(logs))

    def test_simulated_server_records_keep_flash_when_h5_missing(self) -> None:
        from src.accounts.login import parse_login_response

        xml = """<r><c>ok</c><d>acc</d><u>user-1</u><sid>sid-1</sid>
<svr>203.0.113.10:8001:0:;203.0.113.11:8001:443:h5.example;203.0.113.12:8001:443;203.0.113.13:8001:443:h5.example:extra</svr>
<zn>59 库斯特星/0/0/0/0/0/1;39 伊莎女王星/1/0/0/0/0/2;7 跳过/2/0/0/0/0/3</zn></r>"""
        result = parse_login_response(xml)
        self.assertTrue(result.success)
        by_index = {zone.zone_index: zone for zone in result.zone_list}
        flash_only = by_index[59]
        self.assertEqual(flash_only.host, "203.0.113.10")
        self.assertEqual(flash_only.flash_port, 8001)
        self.assertEqual(flash_only.port, 0)
        self.assertEqual(flash_only.domain, "")
        self.assertEqual(flash_only.zone_name, "库斯特星")
        both = by_index[39]
        self.assertEqual(both.domain, "h5.example")
        self.assertEqual(both.port, 443)
        self.assertNotIn(7, by_index)
        self.assertIn("3 字段 1 条", result.server_parse_summary)
        self.assertIn("5 字段 1 条", result.server_parse_summary)
        self.assertNotIn("sid-1", result.server_parse_summary)

    async def test_simulated_missing_h5_does_not_invent_websocket(self) -> None:
        from src.accounts.login import ZoneData
        from src.network import socket_client

        calls = []

        class FakeSocket:
            def __init__(self, *_args, **_kwargs):
                self.disconnect_reason = "TCP 安全策略等待超过 5 秒，未收到以空字节结束的策略，已停止连接"

            async def connect_tcp(self, host, port, timeout=15.0, policy_timeout=None):
                calls.append(("tcp", host, port, policy_timeout))
                return False

            async def connect_ws(self, domain, port, timeout=2):
                calls.append(("ws", domain, port))
                return True

            async def login(self, *_args):
                raise AssertionError("login")

            async def close(self):
                return None

        zone = ZoneData(59, "库斯特星", "203.0.113.10", 0, flash_port=8001, domain="", server_index=0)
        original = socket_client.GameSocket
        socket_client.GameSocket = FakeSocket
        try:
            notes = []
            sock = await socket_client.login_and_connect(
                zone, "user-field", "sid-field", network_mode="system", stage_notes=notes
            )
        finally:
            socket_client.GameSocket = original
        self.assertIsNone(sock)
        self.assertEqual(calls, [("tcp", "203.0.113.10", 8001, None)])
        text = "；".join(notes)
        self.assertIn("h5_port=0", text)
        self.assertIn("domain=空", text)
        self.assertIn("host=203.0.113.10", text)
        self.assertNotIn("ws", [item[0] for item in calls])

    def test_simulated_canonical_login_frame(self) -> None:
        import struct

        from src.config import UpCmd
        from src.protocol.message_codec import build_login_message

        zone = "12 依依星"
        body = build_login_message(zone, "user-field", "sid-field")
        frame = struct.pack(">I", len(body) + 4) + struct.pack(">I", 0) + body
        self.assertEqual(struct.unpack(">I", frame[:4])[0], len(body) + 4)
        self.assertEqual(struct.unpack(">I", frame[4:8])[0], 0)
        self.assertEqual(body[0], 0)
        self.assertEqual(struct.unpack(">H", body[1:3])[0], UpCmd.Login)
        self.assertEqual(body[3:7], b"\xff\xff\xff\xff")
        self.assertIn(zone.encode(), body)
        self.assertNotIn(b"charId", body)

    async def test_simulated_policy_timeout_and_delayed_allow(self) -> None:
        from src.network.socket_client import (
            PASSIVE_POLICY_BYTE_TIMEOUT,
            _policy_allows_game_socket,
            _read_nul_terminated,
        )

        allow = (
            b'<cross-domain-policy><allow-access-from domain="*" to-ports="8001"/>'
            b"</cross-domain-policy>\x00"
        )
        deny = (
            b'<cross-domain-policy><allow-access-from domain="*" to-ports="1"/>'
            b"</cross-domain-policy>\x00"
        )

        class Reader:
            def __init__(self, data: bytes, delay: float) -> None:
                self.data = data
                self.delay = delay
                self.index = 0

            async def readexactly(self, count: int) -> bytes:
                if self.delay:
                    await asyncio.sleep(self.delay)
                    self.delay = 0
                if self.index + count > len(self.data):
                    raise asyncio.IncompleteReadError(self.data[self.index :], count)
                chunk = self.data[self.index : self.index + count]
                self.index += count
                return chunk

        with self.assertRaises(TimeoutError):
            await _read_nul_terminated(Reader(allow, 0.05).readexactly, 0.01)
        received = await _read_nul_terminated(Reader(allow, 0.05).readexactly, 1)
        self.assertTrue(_policy_allows_game_socket(received, 8001))
        self.assertFalse(_policy_allows_game_socket(received, 9))
        self.assertFalse(_policy_allows_game_socket(deny[:-1], 8001))
        self.assertEqual(PASSIVE_POLICY_BYTE_TIMEOUT, 2.0)

    async def test_simulated_same_socket_pushed_policy_then_canonical_login(self) -> None:
        import struct

        from src.config import MSG_TYPE_SYS, DownCmd, UpCmd
        from src.network.socket_client import GameSocket
        from src.protocol.message_codec import build_message, pack_utf

        accepts = {"count": 0}
        frames: list[bytes] = []
        policy = (
            b'<cross-domain-policy><allow-access-from domain="*" to-ports="*"/>'
            b"</cross-domain-policy>\x00"
        )

        async def handle(reader, writer):
            accepts["count"] += 1
            await asyncio.sleep(0.3)
            writer.write(policy[:20])
            await writer.drain()
            await asyncio.sleep(0.02)
            writer.write(policy[20:])
            await writer.drain()
            frames.append(await asyncio.wait_for(reader.read(256), timeout=2))
            reject = build_message(MSG_TYPE_SYS, DownCmd.logKO, -1, pack_utf("secret-text"))
            writer.write(struct.pack(">I", len(reject)) + reject)
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        socket = GameSocket("simulated-session-value", network_mode="system")
        try:
            accepted = await socket.connect_tcp("127.0.0.1", port, timeout=2)
            self.assertTrue(accepted)
            result = await socket.login("12 依依星", "user-secret", "sid-secret")
        finally:
            await socket.close()
            server.close()
            await server.wait_closed()
        self.assertEqual(accepts["count"], 1)
        frame = frames[0]
        self.assertFalse(frame.startswith(b"<policy-file-request/>"))
        body_len = struct.unpack(">I", frame[:4])[0]
        self.assertEqual(struct.unpack(">I", frame[4:8])[0], 0)
        body = frame[8:8 + body_len - 4]
        self.assertEqual(body[0], 0)
        self.assertEqual(struct.unpack(">H", body[1:3])[0], UpCmd.Login)
        self.assertEqual(body[3:7], b"\xff\xff\xff\xff")
        self.assertIn("12 依依星".encode(), body)
        self.assertIsNone(result)
        self.assertTrue(socket.login_explicit_reject)
        self.assertIn("命令=logKO", socket.last_login_summary)
        self.assertNotIn("secret-text", socket.last_login_summary)
        self.assertNotIn("sid-secret", socket.last_login_summary)

    async def test_simulated_no_pushed_policy_still_uses_one_socket(self) -> None:
        import struct

        from src.config import UpCmd
        from src.network.socket_client import GameSocket

        accepts = {"count": 0}
        frames: list[bytes] = []

        async def handle(reader, writer):
            accepts["count"] += 1
            frames.append(await asyncio.wait_for(reader.read(256), timeout=3))
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        socket = GameSocket("simulated-session-value", network_mode="system")
        try:
            accepted = await socket.connect_tcp("127.0.0.1", port, timeout=2)
            self.assertTrue(accepted)
            result = await socket.login("12 依依星", "user-secret", "sid-secret")
        finally:
            await socket.close()
            server.close()
            await server.wait_closed()
        self.assertEqual(accepts["count"], 1)
        frame = frames[0]
        self.assertFalse(frame.startswith(b"<"))
        self.assertEqual(struct.unpack(">I", frame[4:8])[0], 0)
        self.assertEqual(struct.unpack(">H", frame[9:11])[0], UpCmd.Login)
        self.assertIsNone(result)
        self.assertIn("expected=4, received=0", socket.last_login_summary)
        self.assertIn("不是等满", socket.last_login_summary)

    async def test_simulated_partial_login_header_and_denied_policy(self) -> None:
        from src.network.socket_client import GameSocket

        allow = (
            b'<cross-domain-policy><allow-access-from domain="*" to-ports="*"/>'
            b"</cross-domain-policy>\x00"
        )

        async def allow_then_partial(reader, writer):
            writer.write(allow)
            await writer.drain()
            await asyncio.wait_for(reader.read(256), timeout=2)
            writer.write(b"\x00\x01")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(allow_then_partial, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        socket = GameSocket("simulated-session-value", network_mode="system")
        try:
            self.assertTrue(await socket.connect_tcp("127.0.0.1", port, timeout=2))
            result = await socket.login("1 测试", "user-secret", "sid-secret")
        finally:
            await socket.close()
            server.close()
            await server.wait_closed()
        self.assertIsNone(result)
        self.assertIn("expected=4, received=2", socket.last_login_summary)

        seen = {"extra": b"sent"}
        finished = asyncio.Event()

        async def deny(reader, writer):
            writer.write(
                b'<cross-domain-policy><allow-access-from domain="*" to-ports="1"/>'
                b"</cross-domain-policy>\x00"
            )
            await writer.drain()
            try:
                seen["extra"] = await asyncio.wait_for(reader.read(32), timeout=0.3)
            except TimeoutError:
                seen["extra"] = b""
            writer.close()
            finished.set()

        denied = await asyncio.start_server(deny, "127.0.0.1", 0)
        denied_port = denied.sockets[0].getsockname()[1]
        blocked = GameSocket("simulated-session-value", network_mode="system")
        try:
            accepted = await blocked.connect_tcp("127.0.0.1", denied_port, timeout=2)
            await asyncio.wait_for(finished.wait(), timeout=1)
            self.assertFalse(accepted)
            self.assertIn("未允许", blocked.disconnect_reason)
            self.assertEqual(seen["extra"], b"")
        finally:
            await blocked.close()
            denied.close()
            await denied.wait_closed()

    def test_simulated_login_summary_separates_reject_timeout_and_parse(self) -> None:
        from src.network.socket_client import summarize_login_reply

        common = dict(
            honor_count=0,
            parse_fail_count=0,
            parse_fail_bytes=0,
            parse_fail_type=None,
            closed_note="",
            wait_seconds=15,
        )
        text, rejected = summarize_login_reply(None, body_bytes=0, timed_out=True, **common)
        self.assertIn("没有收到回包", text)
        self.assertFalse(rejected)
        text, rejected = summarize_login_reply(
            {"_cmd": "logKO", "_action": 20021, "msg": "account-secret-text"},
            body_bytes=12,
            timed_out=False,
            **common,
        )
        self.assertTrue(rejected)
        self.assertIn("命令=logKO", text)
        self.assertIn("动作=20021", text)
        self.assertIn("12", text)
        self.assertNotIn("account-secret-text", text)
        text, rejected = summarize_login_reply(
            {"_cmd": "not a safe command", "id": "user-secret"},
            body_bytes=8,
            timed_out=False,
            **common,
        )
        self.assertFalse(rejected)
        self.assertIn("无短命令名", text)
        self.assertNotIn("user-secret", text)
        text, _rejected = summarize_login_reply(
            None,
            body_bytes=0,
            timed_out=False,
            honor_count=0,
            parse_fail_count=2,
            parse_fail_bytes=40,
            parse_fail_type=1,
            closed_note="",
            wait_seconds=15,
        )
        self.assertIn("解析失败2次", text)
        self.assertIn("40字节", text)
        self.assertIn("类型=1", text)

    async def test_simulated_login_rejection_is_not_called_no_reply(self) -> None:
        from src.network.socket_client import GameSocket

        socket = GameSocket("simulated-session-value", network_mode="system")

        async def send(_packet):
            return None

        async def recv(timeout=15):
            return ({"_cmd": "logKO", "_action": 20021, "msg": "sid-secret"}, b"abcdef")

        socket._send_raw = send
        socket._recv_any_packet = recv
        result = await socket.login("59 库斯特星", "user-secret", "sid-secret")
        self.assertIsNone(result)
        self.assertTrue(socket.login_explicit_reject)
        self.assertIn("命令=logKO", socket.last_login_summary)
        self.assertNotIn("sid-secret", socket.last_login_summary)
        self.assertNotIn("user-secret", socket.last_login_summary)

    async def test_simulated_explicit_reject_does_not_open_second_transport(self) -> None:
        from src.accounts.login import ZoneData
        from src.network import socket_client

        calls = []

        class FakeSocket:
            def __init__(self, *_args, **_kwargs):
                self.disconnect_reason = ""
                self.last_login_summary = ""
                self.login_explicit_reject = False

            async def connect_tcp(self, host, port, timeout=15.0, policy_timeout=None):
                calls.append("tcp")
                return True

            async def connect_ws(self, domain, port, timeout=2):
                calls.append("ws")
                return True

            async def login(self, *_args):
                calls.append("login")
                self.last_login_summary = "服务器拒绝游戏登录，命令=logKO，动作=20021，正文4字节。不记录拒绝原文。"
                self.login_explicit_reject = True
                return None

            async def close(self):
                calls.append("close")

        zone = ZoneData(1122, "可拉蟹星", "203.0.113.16", 8101, flash_port=8001, domain="h5.example")
        original = socket_client.GameSocket
        socket_client.GameSocket = FakeSocket
        try:
            notes = []
            sock = await socket_client.login_and_connect(
                zone, "user-field", "sid-field", network_mode="system", stage_notes=notes
            )
        finally:
            socket_client.GameSocket = original
        self.assertIsNone(sock)
        self.assertEqual(calls, ["tcp", "login", "close"])
        self.assertIn("命令=logKO", "；".join(notes))
        self.assertNotIn("sid-field", "；".join(notes))

    def test_simulated_windowed_stdio_none_can_print(self) -> None:
        import sys
        from src.game.eggs.win_stdio_hook import ensure_stdio

        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = None
        sys.stderr = None
        opened = None
        try:
            ensure_stdio()
            opened = sys.stdout
            print("startup-ok")
            sys.stderr.write("startup-err\n")
        finally:
            if opened is not None and opened not in {old_out, old_err}:
                opened.close()
            sys.stdout = old_out
            sys.stderr = old_err


if __name__ == "__main__":
    unittest.main()
