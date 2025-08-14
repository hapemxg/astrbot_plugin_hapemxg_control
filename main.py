# main.py (修正 AttributeError)

import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Union

# 导入AstrBot核心API
from astrbot.api import logger
from astrbot.api.message_components import Plain
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register("remote_controller", "YourName", "跨会话消息控制插件", "1.0.0")
class RemoteControlPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 状态存储: { "控制端SID": [被拉取的消息事件1, 消息事件2, ...], ... }
        self.fetched_sessions: Dict[str, List[AstrMessageEvent]] = {}

    def _parse_time_str(self, time_str: str) -> Optional[timedelta]:
        """将 '1h', '30m', '10s' 格式的字符串解析为 timedelta 对象"""
        match = re.match(r"(\d+)([hms])", time_str.lower())
        if not match:
            return None
        value, unit = int(match.group(1)), match.group(2)
        if unit == 'h':
            return timedelta(hours=value)
        if unit == 'm':
            return timedelta(minutes=value)
        if unit == 's':
            return timedelta(seconds=value)
        return None

    @filter.command("fetch")
    async def fetch_messages(self, event: AstrMessageEvent, sid: str, count_or_time: str):
        """
        /fetch [SID] [数量 或 时间] - 从指定会话(SID)拉取最近的消息。
        示例:
        /fetch group_12345 10  (拉取最近10条)
        /fetch private_67890 5m (拉取最近5分钟)
        """
        # --- 核心修正点 1 ---
        # 从方法调用改为属性访问
        control_sid = event.unified_msg_origin
        
        limit = 0
        since = None

        try:
            limit = int(count_or_time)
            if limit <= 0 or limit > 50:
                yield event.plain_result("错误：拉取数量必须在 1 到 50 之间。")
                return
        except ValueError:
            delta = self._parse_time_str(count_or_time)
            if delta:
                since = datetime.now() - delta
            else:
                yield event.plain_result(f"错误：无法识别的数量或时间格式 '{count_or_time}'。请使用数字或如 '5m', '1h' 的格式。")
                return

        try:
            messages = await self.context.get_message_history(sid, limit=limit, since=since)
        except Exception as e:
            logger.error(f"无法从 SID '{sid}' 拉取消息: {e}")
            yield event.plain_result(f"错误：无法从 SID '{sid}' 拉取消息。请检查SID是否正确以及Bot是否有权访问。")
            return

        if not messages:
            yield event.plain_result(f"在 SID '{sid}' 中没有找到符合条件的消息。")
            return

        messages.reverse()
        self.fetched_sessions[control_sid] = messages
        
        response_lines = [f"已从 {sid} 成功拉取 {len(messages)} 条消息:"]
        for i, msg_event in enumerate(messages, 1):
            sender_name = msg_event.get_sender_name()
            content_preview = msg_event.message_str[:30] + '...' if len(msg_event.message_str) > 30 else msg_event.message_str
            response_lines.append(f"{i}. [{sender_name}]: {content_preview}")
        
        response_lines.append("\n使用 /reply [编号] [内容] 来回复指定消息。")
        yield event.plain_result("\n".join(response_lines))

    @filter.command("reply")
    async def reply_to_message(self, event: AstrMessageEvent, index: int, *content: str):
        """
        /reply [编号] [内容 或 'llm'] - 回复已拉取的消息。
        示例:
        /reply 1 你好啊
        /reply 2 llm  (使用LLM智能回复)
        """
        # --- 核心修正点 2 ---
        # 同样，从方法调用改为属性访问
        control_sid = event.unified_msg_origin
        content_str = " ".join(content)

        if control_sid not in self.fetched_sessions or not self.fetched_sessions[control_sid]:
            yield event.plain_result("请先使用 /fetch 指令拉取消息，再进行回复。")
            return
        
        if not (1 <= index <= len(self.fetched_sessions[control_sid])):
            yield event.plain_result(f"错误：编号 {index} 无效。有效编号范围是 1 到 {len(self.fetched_sessions[control_sid])}。")
            return

        target_event = self.fetched_sessions[control_sid][index - 1]
        
        reply_chain = []
        
        if content_str.lower() == 'llm':
            yield event.plain_result(f"正在请求 LLM 为消息 {index} 生成回复，请稍候...")
            try:
                reply_chain = await self.context.llm.ask(target_event.get_messages())
            except Exception as e:
                logger.error(f"LLM 调用失败: {e}")
                yield event.plain_result("LLM 服务调用失败，请检查后台配置或联系管理员。")
                return
        else:
            reply_chain = [Plain(content_str)]

        try:
            await self.context.send_message(
                chain=reply_chain, 
                origin=target_event.unified_msg_origin, # 这里本来就是正确的属性访问
                at_sender=True
            )
            # --- 核心修正点 3 ---
            # 确认消息中的 SID 也应使用属性访问
            yield event.plain_result(f"已成功向会话 {target_event.unified_msg_origin} 中的用户 {target_event.get_sender_name()} 发送回复。")
        except Exception as e:
            logger.error(f"回复消息失败: {e}")
            yield event.plain_result(f"错误：回复消息失败，可能是因为权限不足或目标会话已失效。")