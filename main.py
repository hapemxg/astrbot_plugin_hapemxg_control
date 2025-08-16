# main.py
"""
一个集成了数据模型、服务逻辑和插件主类的单文件 AstrBot 插件。

v2.9.0 (独立人格版):
- [核心修改] /reply LLM 功能不再从全局上下文中动态获取人格，而是从本插件的配置文件中读取独立的 'main_persona_prompt'。
- [目的] 此修改旨在解决因其他插件（如表情包管理器）修改全局人格配置而导致的“状态污染”问题，确保远程控制回复的纯粹性和可预测性。
- [BUG修复] 修正了管理员权限的实现方式，遵循官方文档使用 @filter.permission_type(filter.PermissionType.ADMIN)。
- [权限增强] /fetch 和 /reply 指令现在仅限机器人管理员使用。
- [功能新增] /reply <编号> LLM <额外指令> 功能实现。允许管理员在生成回复时向LLM附加临时指令。
"""

import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

# =============================================================================
# AstrBot 核心 API 导入
# =============================================================================
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Reply, Plain
from astrbot.api.star import Context, Star, register


# =============================================================================
# 1. 常量定义 (Constants)
# =============================================================================
MAX_FETCH_COUNT = 100
DEFAULT_FETCH_COUNT = 20
CONTENT_PREVIEW_LENGTH = 40
LLM_CONTEXT_WINDOW = 3


# =============================================================================
# 2. 自定义异常 (Custom Exceptions)
# =============================================================================
class RemoteControlError(Exception):
    """插件特定的基础异常类，便于统一捕获。"""
    pass

class FetchError(RemoteControlError):
    """消息拉取失败时抛出。"""
    pass

class SidParseError(RemoteControlError):
    """SID 解析失败时抛出。"""
    pass

class GenerationError(RemoteControlError):
    """LLM生成回复失败时抛出。"""
    pass


# =============================================================================
# 3. 数据模型 (Data Models)
# =============================================================================
@dataclass(frozen=True)
class FetchedMessage:
    """
    封装一条被拉取的消息。
    """
    original_raw_event: dict
    display_index: int
    sender_name: str
    content_preview: str

@dataclass
class SessionCache:
    """管理单个控制端拉取的所有消息。"""
    controller_sid: str
    target_sid: str
    fetched_messages: List[FetchedMessage] = field(default_factory=list)
    fetch_time: datetime = field(default_factory=datetime.now)

    def get_message_by_index(self, index: int) -> Optional[FetchedMessage]:
        if 1 <= index <= len(self.fetched_messages):
            return self.fetched_messages[index - 1]
        return None
    
    def get_message_with_context(self, index: int, window_size: int) -> List[FetchedMessage]:
        """获取指定索引的消息及其前的几条消息作为上下文。"""
        if not (1 <= index <= len(self.fetched_messages)):
            return []
        
        start_index = max(0, index - window_size)
        return self.fetched_messages[start_index:index]

    def is_empty(self) -> bool:
        return not self.fetched_messages


# =============================================================================
# 4. 状态管理器 (State Management)
# =============================================================================
class SessionState:
    """管理整个插件的状态，实现会话隔离。"""
    def __init__(self):
        self._sessions: Dict[str, SessionCache] = {}

    def get(self, controller_sid: str) -> Optional[SessionCache]:
        return self._sessions.get(controller_sid)

    def set(self, controller_sid: str, cache: SessionCache):
        self._sessions[controller_sid] = cache


# =============================================================================
# 5. 工具函数 (Utility Functions)
# =============================================================================
def parse_time_str(time_str: str) -> Optional[timedelta]:
    """将 '1h', '30m', '10s' 格式的字符串解析为 timedelta 对象。"""
    match = re.match(r"(\d+)([hms])", time_str.lower())
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(2)
    if unit == 'h': return timedelta(hours=value)
    if unit == 'm': return timedelta(minutes=value)
    if unit == 's': return timedelta(seconds=value)
    return None

def parse_sid(sid: str) -> Tuple[str, str, str]:
    """解析 SID 字符串，例如 'aiocqhttp:GroupMessage:763047561'。"""
    parts = sid.split(':')
    if len(parts) != 3:
        raise SidParseError(f"SID '{sid}' 格式不正确，应为 'platform:type:id'。")
    return parts[0], parts[1], parts[2]

def stringify_message(message: any) -> str:
    """将 go-cqhttp 返回的 message 字段（可能是字符串或列表）转换为纯文本字符串。"""
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        text_parts = []
        for segment in message:
            if segment.get('type') == 'text':
                text_parts.append(segment.get('data', {}).get('text', ''))
        return ''.join(text_parts)
    return ''


# =============================================================================
# 6. 服务层 (Service Layer)
# =============================================================================
class MessageService:
    """负责消息处理的核心服务，直接与平台 API 交互。"""
    # [MODIFIED] __init__ 方法接收新增的 main_persona_prompt 参数
    def __init__(self, state: SessionState, context: Context, config: dict, main_persona_prompt: str):
        self.state = state
        self.context = context
        self.config = config
        self.llm_provider_name = self.config.get("llm_provider_name")
        # [MODIFIED] 保存从配置文件传入的独立人格
        self.main_persona_prompt = main_persona_prompt

    async def fetch_history(self, event: AstrMessageEvent, controller_sid: str, target_sid: str, count: int, since: Optional[datetime]) -> SessionCache:
        platform, msg_type, target_id = parse_sid(target_sid)
        if platform != "aiocqhttp":
            raise FetchError(f"暂不支持从平台 '{platform}' 拉取消息。")
        bot = event.bot
        raw_messages: List[dict] = []
        try:
            if msg_type in ["GroupMessage", "TempMessage"]:
                result = await bot.get_group_msg_history(group_id=int(target_id), count=count)
                raw_messages = result.get("messages", []) if result else []
            elif msg_type == "PrivateMessage":
                result = await bot.get_friend_msg_history(user_id=int(target_id), count=count)
                raw_messages = result.get("messages", []) if result else []
            else:
                raise FetchError(f"不支持的消息类型 '{msg_type}'。")
        except Exception as e:
            logger.error(f"调用平台 API 从 SID '{target_sid}' 拉取消息失败: {e}", exc_info=True)
            raise FetchError(f"无法从 SID '{target_sid}' 拉取消息。请检查SID是否正确以及Bot是否有权访问。")
        if not raw_messages:
            raise FetchError(f"在 SID '{target_sid}' 中没有找到任何消息。")
        if since:
            raw_messages = [msg for msg in raw_messages if datetime.fromtimestamp(msg.get('time', 0)) >= since]
        if not raw_messages:
            raise FetchError(f"在指定时间范围内没有找到任何消息。")
        raw_messages.reverse()
        fetched_messages = []
        for i, msg_dict in enumerate(raw_messages, 1):
            sender_name = msg_dict.get('sender', {}).get('nickname', '未知发信人')
            content_text = stringify_message(msg_dict.get('message', ''))
            preview = (content_text[:CONTENT_PREVIEW_LENGTH] + '...') if len(content_text) > CONTENT_PREVIEW_LENGTH else content_text
            fetched_messages.append(FetchedMessage(
                original_raw_event=msg_dict,
                display_index=i,
                sender_name=sender_name,
                content_preview=preview or "[非文本消息]",
            ))
        cache = SessionCache(controller_sid, target_sid, fetched_messages)
        self.state.set(controller_sid, cache)
        return cache

    async def send_reply(self, event: AstrMessageEvent, controller_sid: str, message_index: int, reply_content: str):
        cache = self.state.get(controller_sid)
        if not cache or cache.is_empty():
            raise RemoteControlError("请先使用 /fetch 指令拉取消息。")
        target_message = cache.get_message_by_index(message_index)
        if not target_message:
            raise RemoteControlError(f"编号 {message_index} 无效。有效范围是 1 到 {len(cache.fetched_messages)}。")
        
        raw_event = target_message.original_raw_event
        message_id = raw_event.get('message_id')
        if not message_id:
            raise RemoteControlError("无法获取目标消息的ID，无法引用回复。")
        
        reply_chain = [Reply(id=message_id), Plain(reply_content)]
        bot = event.bot
        _, msg_type, target_id = parse_sid(cache.target_sid)
        
        try:
            if msg_type in ["GroupMessage", "TempMessage"]:
                await bot.send_group_msg(group_id=int(target_id), message=reply_chain)
            elif msg_type == "PrivateMessage":
                await bot.send_private_msg(user_id=int(target_id), message=[Plain(reply_content)])
            else:
                raise RemoteControlError("无法确定回复目标（非群聊或私聊）。")
        except Exception as e:
            logger.error(f"回复消息到 SID '{cache.target_sid}' 失败: {e}", exc_info=True)
            raise RemoteControlError("回复消息失败，可能是权限不足或目标会话已失效。")

    async def generate_and_send_llm_reply(self, event: AstrMessageEvent, controller_sid: str, message_index: int, extra_instruction: Optional[str] = None):
        """生成并发送由LLM驱动的回复。"""
        cache = self.state.get(controller_sid)
        if not cache or cache.is_empty():
            raise RemoteControlError("请先使用 /fetch 指令拉取消息。")

        provider = None
        if self.llm_provider_name:
            provider = self.context.get_provider_by_id(self.llm_provider_name)
            if not provider:
                raise GenerationError(f"配置的LLM提供商 '{self.llm_provider_name}' 未找到。")
        else:
            logger.debug("llm_provider_name 未配置，尝试获取当前正在使用的提供商...")
            provider = self.context.get_using_provider()
            if not provider:
                raise GenerationError("未配置llm_provider_name，且框架没有设置默认或当前正在使用的聊天提供商。")

        provider_name_for_log = type(provider).__name__
        logger.debug(f"将使用提供商 '{provider_name_for_log}' 生成LLM回复。")

        llm_context_window = self.config.get("llm_context_window", 3)
        message_context = cache.get_message_with_context(message_index, llm_context_window)
        if not message_context:
            raise RemoteControlError(f"编号 {message_index} 无效。")
        
        target_message = message_context[-1]

        # [MODIFIED] 直接使用从配置文件中读取的独立人格，不再动态获取
        persona_prompt = self.main_persona_prompt
        
        history_str = "\n".join(
            f"[{msg.sender_name}]: {stringify_message(msg.original_raw_event.get('message', ''))}"
            for msg in message_context
        )
        
        task_description = f'任务：请你代入你的角色，针对最后一条消息（来自"{target_message.sender_name}"）生成一个自然、直接、且符合上下文的回复。'
        if extra_instruction:
            task_description += f'\n管理员对本次回复有如下指示，请务必遵守：“{extra_instruction}”。'

        user_prompt = f"""以下是最近的一段聊天记录：
---
{history_str}
---
{task_description}
你的回复应该直接就是聊天内容，不要包含如“回复：”或任何额外解释。"""

        try:
            logger.debug(f"LLM System Prompt (Independent):\n{persona_prompt}")
            logger.debug(f"LLM User Prompt:\n{user_prompt}")
            
            response = await provider.text_chat(
                prompt=user_prompt,
                system_prompt=persona_prompt
            )
            generated_content = response.completion_text.strip()
            if not generated_content:
                raise GenerationError("LLM返回了空内容。")
        except Exception as e:
            logger.error(f"调用LLM提供商 '{provider_name_for_log}' 失败: {e}", exc_info=True)
            raise GenerationError(f"LLM生成回复时发生错误: {e}")

        logger.info(f"LLM生成回复成功，将发送至 {cache.target_sid}。内容: {generated_content[:50]}...")
        await self.send_reply(event, controller_sid, message_index, generated_content)

    # [REMOVED] _get_active_persona_prompt 方法已被移除，因为它不再被需要


# =============================================================================
# 7. 插件主类 (Plugin Class - The Entry Point)
# =============================================================================
@register("remote_controller", "YourName", "跨会话消息控制插件 (v2.9.0 独立人格)", "2.9.0")
class RemoteControlPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.state = SessionState()
        self.config = config 
        self.llm_context_window = self.config.get("llm_context_window", 3)
        self.default_fetch_count = self.config.get("default_fetch_count", 20)
        self.max_fetch_count = self.config.get("max_fetch_count", 100)
        
        # [MODIFIED] 从本插件的配置中读取独立的人格提示词
        self.main_persona_prompt = self.config.get("main_persona_prompt", "你是一个友好、乐于助人的AI助手。")
        
        # [MODIFIED] 将读取到的人格提示词传递给服务层
        self.message_service = MessageService(self.state, self.context, self.config, self.main_persona_prompt)

    def _format_fetch_success_message(self, cache: SessionCache) -> str:
        lines = [f"已从 {cache.target_sid} 成功拉取 {len(cache.fetched_messages)} 条消息:"]
        for msg in cache.fetched_messages:
            lines.append(f"{msg.display_index}. [{msg.sender_name}]: {msg.content_preview}")
        lines.append("\n使用 /reply <编号> <内容> 来回复。")
        lines.append("使用 /reply <编号> LLM [额外指令] 来让AI生成回复。")
        return "\n".join(lines)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("fetch")
    async def fetch_messages(self, event: AstrMessageEvent, sid: Optional[str] = None, count_or_time: Optional[str] = None):
        if not sid:
            yield event.plain_result("指令格式错误。\n用法: /fetch <SID> [数量或时间]")
            return
        
        count_or_time_str = count_or_time if count_or_time is not None else str(self.default_fetch_count)
        limit = 0
        since = None
        try:
            limit = int(count_or_time_str)
            if not (1 <= limit <= self.max_fetch_count):
                yield event.plain_result(f"错误：拉取数量必须在 1 到 {self.max_fetch_count} 之间。")
                return
        except ValueError:
            delta = parse_time_str(count_or_time_str)
            if delta:
                since = datetime.now() - delta
                limit = self.max_fetch_count
            else:
                yield event.plain_result(f"错误：无法识别的数量或时间格式 '{count_or_time_str}'。")
                return
        
        try:
            cache = await self.message_service.fetch_history(event, event.unified_msg_origin, sid, limit, since)
            yield event.plain_result(self._format_fetch_success_message(cache))
        except RemoteControlError as e:
            yield event.plain_result(str(e))
        except Exception as e:
            logger.error(f"处理 /fetch 命令 (SID: {sid}) 时发生未知错误: {e}", exc_info=True)
            yield event.plain_result("发生了一个内部错误，请检查日志或联系管理员。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("reply")
    async def reply_to_message(self, event: AstrMessageEvent):
        """
        /reply <编号> <内容> - 回复已拉取的消息。
        /reply <编号> LLM [额外指令] - 让AI生成并回复消息。
        """
        parts = event.message_str.split(' ', 1)
        if len(parts) < 2 or not parts[1]:
            yield event.plain_result("指令格式错误。\n用法: /reply <编号> <内容|LLM [额外指令]>")
            return
        
        args_text = parts[1]
        arg_parts = args_text.split(' ', 1)
        
        index_str = arg_parts[0]
        content = arg_parts[1] if len(arg_parts) > 1 else ""

        if not content:
             yield event.plain_result("指令格式错误，缺少回复内容或LLM关键词。\n用法: /reply <编号> <内容|LLM [额外指令]>")
             return
            
        try:
            index = int(index_str)
        except ValueError:
            yield event.plain_result(f"指令格式错误：编号 '{index_str}' 不是一个有效的数字。")
            return

        try:
            if content.upper().startswith('LLM'):
                llm_parts = content.split(' ', 1)
                extra_instruction = llm_parts[1] if len(llm_parts) > 1 else None
                
                yield event.plain_result(f"🧠 正在为编号 {index} 的消息生成AI回复，请稍候...")
                await self.message_service.generate_and_send_llm_reply(event, event.unified_msg_origin, index, extra_instruction)
                yield event.plain_result(f"✅ 已通过LLM向编号 {index} 的消息发送回复。")
            else:
                await self.message_service.send_reply(event, event.unified_msg_origin, index, content)
                yield event.plain_result(f"✅ 已向编号 {index} 的消息发送回复。")
        except RemoteControlError as e:
            yield event.plain_result(f"操作失败: {e}")
        except Exception as e:
            logger.error(f"处理 /reply 命令 (Index: {index}) 时发生未知错误: {e}", exc_info=True)
            yield event.plain_result("发生了一个内部错误，请检查日志或联系管理员。")