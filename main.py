import json
import time
import datetime
from typing import Dict
from dataclasses import dataclass

import astrbot.api.star as star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api import logger


@dataclass
class JudgeResult:
    """判断结果数据类"""
    relevance: float = 0.0
    willingness: float = 0.0
    social: float = 0.0
    timing: float = 0.0
    continuity: float = 0.0
    reasoning: str = ""
    should_reply: bool = False
    confidence: float = 0.0
    overall_score: float = 0.0
    related_messages: list = None

    def __post_init__(self):
        if self.related_messages is None:
            self.related_messages = []


@dataclass
class ChatState:
    """群聊状态数据类"""
    energy: float = 1.0
    last_reply_time: float = 0.0
    last_reset_date: str = ""
    total_messages: int = 0
    total_replies: int = 0


class HeartflowPlugin(star.Star):

    def __init__(self, context: star.Context, config):
        super().__init__(context)
        self.config = config

        self.judge_provider_name = self.config.get("judge_provider_name", "")
        self.reply_threshold = self.config.get("reply_threshold", 0.6)
        self.energy_decay_rate = self.config.get("energy_decay_rate", 0.1)
        self.energy_recovery_rate = self.config.get("energy_recovery_rate", 0.02)
        self.context_messages_count = self.config.get("context_messages_count", 5)
        self.whitelist_enabled = self.config.get("whitelist_enabled", False)
        self.chat_whitelist = self.config.get("chat_whitelist", [])
        self.log_judge_details = self.config.get("log_judge_details", False)

        personas_config = self.config.get("summarized_personas")
        self.summarized_personas = {}

        if isinstance(personas_config, str) and personas_config.strip():
            try:
                parsed_data = json.loads(personas_config)
                if isinstance(parsed_data, dict):
                    self.summarized_personas = parsed_data
                    logger.debug("已从文本配置成功加载 'summarized_personas'。")
                else:
                    logger.warning(f"配置项 'summarized_personas' 的内容是有效的JSON，但不是一个字典（而是 {type(parsed_data).__name__}），将使用空配置。")
            except json.JSONDecodeError:
                logger.warning("无法解析配置项 'summarized_personas' 的JSON字符串，请检查其格式。将使用空配置。")
        elif isinstance(personas_config, dict):
            self.summarized_personas = personas_config

        self.chat_states: Dict[str, ChatState] = {}
        self.weights = {
            "relevance": 0.25,
            "willingness": 0.2,
            "social": 0.2,
            "timing": 0.15,
            "continuity": 0.2
        }

        logger.info("心流插件已初始化")

    async def _get_active_persona_prompt(self, event: AstrMessageEvent) -> str:
        """
        [核心重构] 获取当前激活的人格提示词。
        该方法是获取人格的唯一入口，它会智能地选择使用精简版还是完整版。
        
        处理逻辑:
        1. 获取当前会话的人格ID。
        2. 使用人格ID优先在 self.summarized_personas (精简人格配置) 中查找。
        3. 如果找到，则返回精简版提示词。
        4. 如果未找到，则根据人格ID查找完整的原始提示词并返回。
        5. 如果人格ID无效或未找到任何提示词，则返回空字符串。

        Args:
            event (AstrMessageEvent): 消息事件对象。

        Returns:
            str: 最终决定使用的提示词（精简版或完整版）。
        """
        try:
            # 步骤 1: 获取人格ID
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            
            # 默认使用全局默认人格
            persona_id = self.context.provider_manager.selected_default_persona["name"]

            if curr_cid:
                conversation = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, curr_cid)
                if conversation and conversation.persona_id:
                    # 如果会话中指定了人格，则使用它
                    persona_id = conversation.persona_id

            if not persona_id:
                logger.debug("无法确定人格ID，不使用任何人格提示词。")
                return ""
            if persona_id == "[%None]":
                logger.debug("用户已显式取消人格，不使用任何人格提示词。")
                return ""

            # 步骤 2 & 3: 优先查找并返回精简版人格
            summarized_prompt = self.summarized_personas.get(persona_id)
            if summarized_prompt and isinstance(summarized_prompt, str):
                logger.debug(f"为人格 '{persona_id}' 找到了并使用了配置好的 [精简] 提示词。")
                return summarized_prompt
            
            # 步骤 4: 未找到精简版，查找并返回完整版人格
            logger.debug(f"未找到人格 '{persona_id}' 的精简提示词，尝试查找其完整版。")
            full_prompt = self._get_full_persona_prompt_by_name(persona_id)
            if full_prompt:
                logger.debug(f"为人格 '{persona_id}' 使用了 [完整] 的原始提示词。")
            
            return full_prompt

        except Exception as e:
            logger.error(f"获取激活的人格提示词时发生异常: {e}")
            return ""

    def _get_full_persona_prompt_by_name(self, persona_name: str) -> str:
        """
        根据人格名称(ID)从provider_manager中获取完整的人格提示词。
        这是一个纯粹的辅助函数。
        """
        try:
            for persona in self.context.provider_manager.personas:
                if persona.get("name") == persona_name:
                    return persona.get("prompt", "")
            logger.warning(f"无法在provider_manager中找到名为 '{persona_name}' 的人格。")
            return ""
        except Exception as e:
            logger.error(f"根据名称查找完整人格提示词时发生异常: {e}")
            return ""
            
    async def judge_with_tiny_model(self, event: AstrMessageEvent) -> JudgeResult:
        """使用小模型进行智能判断"""

        if not self.judge_provider_name:
            logger.warning("小参数判断模型提供商名称未配置，跳过心流判断")
            return JudgeResult(should_reply=False, reasoning="提供商未配置")

        try:
            judge_provider = self.context.get_provider_by_id(self.judge_provider_name)
            if not judge_provider:
                logger.warning(f"未找到提供商: {self.judge_provider_name}")
                return JudgeResult(should_reply=False, reasoning=f"提供商不存在: {self.judge_provider_name}")
        except Exception as e:
            logger.error(f"获取提供商失败: {e}")
            return JudgeResult(should_reply=False, reasoning=f"获取提供商失败: {str(e)}")

        # 1. 使用重构后的单一方法获取最终的人格提示词
        persona_system_prompt = await self._get_active_persona_prompt(event)
        logger.debug(f"小参数模型最终使用的人格提示词长度: {len(persona_system_prompt)}")

        # 2. 获取其他上下文信息
        chat_state = self._get_chat_state(event.unified_msg_origin)
        chat_context = await self._build_chat_context(event)
        recent_messages = await self._get_recent_messages(event)
        last_bot_reply = await self._get_last_bot_reply(event)

        # 3. 构建判断Prompt模板
        judge_prompt_template = f"""
你是群聊机器人的决策系统，需要判断是否应该主动回复以下消息。

## 当前群聊情况
- 群聊ID: {event.unified_msg_origin}
- 我的精力水平: {chat_state.energy:.1f}/1.0
- 上次发言: {self._get_minutes_since_last_reply(event.unified_msg_origin)}分钟前

## 群聊基本信息
{chat_context}

## 最近{self.context_messages_count}条对话历史
{recent_messages}

## 上次机器人回复
{last_bot_reply if last_bot_reply else "暂无上次回复记录"}

## 待判断消息
发送者: {event.get_sender_name()}
内容: {event.message_str}
时间: {datetime.datetime.now().strftime('%H:%M:%S')}

## 评估要求
请从以下5个维度评估（0-10分），**重要提醒：基于上述机器人角色设定来判断是否适合回复**：
1. **内容相关度**(0-10)：消息是否有趣、有价值、适合我回复
   - **结合机器人角色特点，判断是否符合角色定位**
2. **回复意愿**(0-10)：基于当前状态，我回复此消息的意愿
   - **基于机器人角色设定，判断是否应该主动参与此话题**
3. **社交适宜性**(0-10)：在当前群聊氛围下回复是否合适
   - **考虑机器人角色在群中的定位和表现方式**
4. **时机恰当性**(0-10)：回复时机是否恰当
5. **对话连贯性**(0-10)：当前消息与上次机器人回复的关联程度

**回复阈值**: {self.reply_threshold}

**关联消息筛选要求**：
- 从上面的对话历史中找出与当前消息内容相关的消息
- 直接复制相关消息的完整内容，保持原有格式
- 如果没有相关消息，返回空数组

**重要！！！请严格按照以下JSON格式回复，不要添加任何其他内容：**
{{
    "relevance": 0.0,
    "willingness": 0.0,
    "social": 0.0,
    "timing": 0.0,
    "continuity": 0.0,
    "reasoning": "详细分析原因，说明为什么应该或不应该回复，需要结合机器人角色特点进行分析，特别说明与上次回复的关联性",
    "should_reply": false,
    "confidence": 0.0,
    "related_messages": []
}}
"""

        try:
            # 构建系统提示的逻辑保持不变
            system_prompt_content = "你是一个专业的群聊回复决策系统。你的回复必须是完整的JSON对象，不要包含任何解释性文字！"
            if persona_system_prompt:
                system_prompt_content += f"\n\n你正在为以下角色的机器人做决策，请严格代入其身份和口吻进行分析：\n---角色设定---\n{persona_system_prompt}\n---角色设定---"

            # [核心修复] 将系统提示内容和用户提示模板拼接在一起
            # 这是为了兼容不支持独立 system_prompt 参数的模型
            final_prompt_for_model = f"{system_prompt_content}\n\n{judge_prompt_template}"

            # 调用模型时，只使用 prompt 参数
            llm_response = await judge_provider.text_chat(
                prompt=final_prompt_for_model,
                contexts=await self._get_recent_contexts(event)
            )

            # ... 后续的JSON解析逻辑保持不变 ...
            content = llm_response.completion_text.strip()
            
            try:
                if content.startswith("```json"):
                    content = content.replace("```json", "").replace("```", "").strip()
                elif content.startswith("```"):
                    content = content.replace("```", "").strip()

                judge_data = json.loads(content)

                overall_score = (
                    judge_data.get("relevance", 0) * self.weights["relevance"] +
                    judge_data.get("willingness", 0) * self.weights["willingness"] +
                    judge_data.get("social", 0) * self.weights["social"] +
                    judge_data.get("timing", 0) * self.weights["timing"] +
                    judge_data.get("continuity", 0) * self.weights["continuity"]
                ) / 10.0

                return JudgeResult(
                    relevance=judge_data.get("relevance", 0),
                    willingness=judge_data.get("willingness", 0),
                    social=judge_data.get("social", 0),
                    timing=judge_data.get("timing", 0),
                    continuity=judge_data.get("continuity", 0),
                    reasoning=judge_data.get("reasoning", ""),
                    should_reply=judge_data.get("should_reply", False) and overall_score >= self.reply_threshold,
                    confidence=judge_data.get("confidence", 0.0),
                    overall_score=overall_score,
                    related_messages=judge_data.get("related_messages", [])
                )
            except json.JSONDecodeError as e:
                logger.error(f"小参数模型返回非有效JSON: {content}")
                return JudgeResult(should_reply=False, reasoning=f"JSON解析失败: {str(e)}")

        except Exception as e:
            logger.error(f"小参数模型判断异常: {e}")
            return JudgeResult(should_reply=False, reasoning=f"异常: {str(e)}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1000)
    async def on_group_message(self, event: AstrMessageEvent):
        """群聊消息处理入口"""
        if not self._should_process_message(event):
            return

        try:
            # 1. 小参数模型判断是否需要回复
            judge_result = await self.judge_with_tiny_model(event)

            # 2. 如果开启了详细日志，则无论结果如何都打印
            if self.log_judge_details:
                self._log_decision_details(judge_result, event)

            # 3. 根据判断结果执行后续操作
            if judge_result.should_reply:
                logger.info(f"🔥 心流触发主动回复 | {event.unified_msg_origin[:20]}... | 评分:{judge_result.overall_score:.2f}")
                event.is_at_or_wake_command = True
                self._update_active_state(event, judge_result)
                return
            else:
                logger.debug(f"心流判断不通过 | {event.unified_msg_origin[:20]}... | 评分:{judge_result.overall_score:.2f} | 原因: {judge_result.reasoning[:30]}...")
                self._update_passive_state(event, judge_result)

        except Exception as e:
            logger.error(f"心流插件处理消息异常: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def _log_decision_details(self, result: JudgeResult, event: AstrMessageEvent):
        """
        [新增方法] 格式化并记录详细的决策日志。
        """
        decision_icon = "✅" if result.should_reply else "❌"
        decision_text = "回复" if result.should_reply else "不回复"

        log_message = f"""
--- Heartflow Decision Log ---
- 群聊: {event.unified_msg_origin}
- 消息: "{event.message_str[:50]}..."
- 最终决策: {decision_icon} {decision_text} (综合评分: {result.overall_score:.2f} / 阈值: {self.reply_threshold})
- 决策理由: {result.reasoning}
------------------------------
- 评分详情:
    - 内容相关度: {result.relevance:.1f}
    - 回复意愿: {result.willingness:.1f}
    - 社交适宜性: {result.social:.1f}
    - 时机恰当性: {result.timing:.1f}
    - 对话连贯性: {result.continuity:.1f}
--- End of Log ---
"""
        logger.info(log_message)

    def _should_process_message(self, event: AstrMessageEvent) -> bool:
        """检查是否应该处理这条消息"""
        if not self.config.get("enable_heartflow", False):
            return False
        if event.is_at_or_wake_command:
            logger.debug(f"跳过已被标记为唤醒的消息: {event.message_str}")
            return False
        if self.whitelist_enabled and event.unified_msg_origin not in self.chat_whitelist:
            logger.debug(f"群聊不在白名单中，跳过处理: {event.unified_msg_origin}")
            return False
        if event.get_sender_id() == event.get_self_id():
            return False
        if not event.message_str or not event.message_str.strip():
            return False
        return True

    def _get_chat_state(self, chat_id: str) -> ChatState:
        """获取群聊状态"""
        if chat_id not in self.chat_states:
            self.chat_states[chat_id] = ChatState()
        today = datetime.date.today().isoformat()
        state = self.chat_states[chat_id]
        if state.last_reset_date != today:
            state.last_reset_date = today
            state.energy = min(1.0, state.energy + 0.2)
        return state

    def _get_minutes_since_last_reply(self, chat_id: str) -> int:
        """获取距离上次回复的分钟数"""
        chat_state = self._get_chat_state(chat_id)
        if chat_state.last_reply_time == 0:
            return 999
        return int((time.time() - chat_state.last_reply_time) / 60)

    async def _get_recent_contexts(self, event: AstrMessageEvent) -> list:
        """获取最近的对话上下文（过滤函数调用）"""
        try:
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            if not curr_cid: return []
            conversation = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, curr_cid)
            if not conversation or not conversation.history: return []
            context = json.loads(conversation.history)
            recent_context = context[-self.context_messages_count:]
            filtered_context = []
            for msg in recent_context:
                role, content = msg.get("role"), msg.get("content")
                if role in ["user", "assistant"] and content and isinstance(content, str):
                    filtered_context.append({"role": role, "content": content})
            return filtered_context
        except Exception as e:
            logger.debug(f"获取对话上下文失败: {e}")
            return []

    async def _build_chat_context(self, event: AstrMessageEvent) -> str:
        """构建群聊上下文"""
        chat_state = self._get_chat_state(event.unified_msg_origin)
        return f"""最近活跃度: {'高' if chat_state.total_messages > 100 else '中' if chat_state.total_messages > 20 else '低'}
历史回复率: {(chat_state.total_replies / max(1, chat_state.total_messages) * 100):.1f}%
当前时间: {datetime.datetime.now().strftime('%H:%M')}"""

    async def _get_recent_messages(self, event: AstrMessageEvent) -> str:
        """获取最近的消息历史文本"""
        try:
            contexts = await self._get_recent_contexts(event)
            if not contexts: return "暂无对话历史"
            messages_text = []
            for msg in contexts:
                messages_text.append(f"{msg['role']}: {msg['content']}")
            return "\n---\n".join(messages_text)
        except Exception as e:
            logger.debug(f"获取消息历史失败: {e}")
            return "暂无对话历史"

    async def _get_last_bot_reply(self, event: AstrMessageEvent) -> str:
        """获取上次机器人的回复消息"""
        try:
            contexts = await self._get_recent_contexts(event)
            for msg in reversed(contexts):
                if msg.get("role") == "assistant" and msg.get("content", "").strip():
                    return msg["content"]
            return None
        except Exception as e:
            logger.debug(f"获取上次bot回复失败: {e}")
            return None

    def _update_active_state(self, event: AstrMessageEvent, judge_result: JudgeResult):
        """更新主动回复状态"""
        chat_state = self._get_chat_state(event.unified_msg_origin)
        chat_state.last_reply_time = time.time()
        chat_state.total_replies += 1
        chat_state.total_messages += 1
        chat_state.energy = max(0.1, chat_state.energy - self.energy_decay_rate)
        logger.debug(f"更新主动状态: {event.unified_msg_origin[:20]}... | 精力: {chat_state.energy:.2f}")

    def _update_passive_state(self, event: AstrMessageEvent, judge_result: JudgeResult):
        """更新被动状态（未回复）"""
        chat_state = self._get_chat_state(event.unified_msg_origin)
        chat_state.total_messages += 1
        chat_state.energy = min(1.0, chat_state.energy + self.energy_recovery_rate)
        logger.debug(f"更新被动状态: {event.unified_msg_origin[:20]}... | 精力: {chat_state.energy:.2f}")

    @filter.command("heartflow")
    async def heartflow_status(self, event: AstrMessageEvent):
        """查看心流状态"""
        chat_id = event.unified_msg_origin
        chat_state = self._get_chat_state(chat_id)
        status_info = f"""
🔮 心流状态报告
📊 **当前状态**
- 群聊ID: {chat_id}
- 精力水平: {chat_state.energy:.2f}/1.0 {'🟢' if chat_state.energy > 0.7 else '🟡' if chat_state.energy > 0.3 else '🔴'}
- 上次回复: {self._get_minutes_since_last_reply(chat_id)}分钟前
📈 **历史统计**
- 总消息数: {chat_state.total_messages}
- 总回复数: {chat_state.total_replies}
- 回复率: {(chat_state.total_replies / max(1, chat_state.total_messages) * 100):.1f}%
⚙️ **配置参数**
- 回复阈值: {self.reply_threshold}
- 判断提供商: {self.judge_provider_name}
- 白名单模式: {'✅ 开启' if self.whitelist_enabled else '❌ 关闭'}
- 白名单群聊数: {len(self.chat_whitelist) if self.whitelist_enabled else 0}
🎯 **评分权重**
- 内容相关度: {self.weights['relevance']:.0%} | 回复意愿: {self.weights['willingness']:.0%}
- 社交适宜性: {self.weights['social']:.0%} | 时机恰当性: {self.weights['timing']:.0%}
- 对话连贯性: {self.weights['continuity']:.0%}
🎯 **插件状态**: {'✅ 已启用' if self.config.get('enable_heartflow', False) else '❌ 已禁用'}
"""
        event.set_result(event.plain_result(status_info))

    @filter.command("heartflow_reset")
    async def heartflow_reset(self, event: AstrMessageEvent):
        """重置心流状态"""
        chat_id = event.unified_msg_origin
        if chat_id in self.chat_states:
            del self.chat_states[chat_id]
        event.set_result(event.plain_result("✅ 心流状态已重置"))
        logger.info(f"心流状态已重置: {chat_id}")