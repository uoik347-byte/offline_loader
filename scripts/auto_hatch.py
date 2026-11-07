"""孵化所有亚比蛋。

协议说明仍以仓库里的这几条发送为准：

- ext 17 ``1-8``：孵化状态，``es`` 里 ``ei`` / ``pmid`` / ``st``（100 可收取）
- ext 17 ``1=1``：未孵化蛋，``es`` 里 ``ei``
- ext 17 ``1-4``：``{"eggId", "id"}``，``id`` 是 1/2/3 号孵化格
- ext 13 ``1223``：``{"loc": 4, "id": pmid}``，回执命令是 ``2223``

实际发送由 ``src.game.eggs`` 执行。超时不是空列表，就绪超时不释放格子，
收取回执必须对上 pmid，并且随后的 1-8 不再包含该蛋。结果不确定时不自动重发。
在旧加载器里选择本脚本仍然会开始；独立窗口另有一次确认。
"""
from __future__ import annotations

from src.config import PROJECT_ROOT
from src.game.eggs.engine import EggEngine
from src.game.eggs.store import JsonHoldStore
from src.game.eggs.transport import ContextTransport
from src.network.context import AppContext

SCRIPT_NAME = "孵化所有亚比蛋"
SCRIPT_DESCRIPTION = "查询未孵化和待收取的蛋，确认占用后再孵化并收取"


async def run(context: AppContext) -> None:
    await context.wait_for_player_initialization()
    context.assert_automation_allowed()
    if not context.socket.connected or context.messages.disconnected.is_set():
        raise ConnectionError("账号连接已断开")
    engine = EggEngine(
        ContextTransport(context, lambda: False, context.log),
        store=JsonHoldStore(PROJECT_ROOT / "egg-hatch-local-state.json"),
        log=context.log,
    )
    try:
        report = await engine.run(confirmed=True)
    finally:
        engine.transport.close()
    if report.kind in {"timeout", "malformed", "disconnected", "blocked", "store"}:
        raise RuntimeError(report.detail)
    context.log(report.detail)
