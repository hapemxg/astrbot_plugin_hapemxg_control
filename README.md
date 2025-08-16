跨会话远程控制插件 (Remote Controller)

赋予机器人管理员“上帝视角”的强大工具。无论你身在何处，都可以通过简单的指令，查看并回复机器人所在任何群组或私聊的消息。

本插件是为 AstrBot 设计的，旨在为机器人管理员提供强大的跨会e话管理能力。当自动回复不足以应对复杂情况时，本插件允许你无缝介入，以手动或 AI 生成的方式进行精准回复。

✨ 功能特性

跨会话消息拉取: 从机器人所在的任意群聊或私聊中，拉取最新的历史消息。

精准引用回复: 对拉取到的任何一条消息进行精确的引用回复，上下文清晰明了。

智能 AI 代回: 调用 LLM (大语言模型)，根据上下文和你的临时指令，智能生成回复内容。

独立人格系统: 使用独立的、在插件配置中定义的人格（Persona），确保 AI 回复的风格稳定，不受其他插件（如表情包插件）的“污染”。

灵活指令注入: 在让 AI 回复时，可以附加临时的额外指令 (如 “用傲娇的语气回复他”)，实现丰富多样的互动效果。

管理员权限限定: 所有核心功能都严格限制为机器人管理员使用，确保安全可控。

⚙️ 配置说明

在 AstrBot 的插件配置页面找到本插件，你可以看到以下配置项：

配置项	类型	描述
llm_provider_name	字符串	用于 /reply ... LLM 指令的语言模型提供商ID。如果留空，将使用框架的默认聊天提供商。
llm_context_window	整数	AI 生成回复时，向前追溯的聊天记录数量。这为 AI 提供了必要的上下文，建议设置为 3-5。
default_fetch_count	整数	使用 /fetch 指令但不指定数量时，默认拉取的消息条数。
max_fetch_count	整数	/fetch 指令单次允许拉取的最大消息条数，用于防止滥用和性能问题。
main_persona_prompt	文本	【核心配置】 这是本插件进行 AI 回复时使用的独立核心人设。请将你的机器人长篇核心人设完整粘贴于此。

为什么 main_persona_prompt 很重要？
为了保证远程控制回复的纯粹性和稳定性，本插件不使用全局的人格设置。这可以有效避免其他插件（例如，为全局人格注入了表情包指令的插件）对远程回复造成干扰，确保 LLM 生成的内容干净、可控。

🚀 使用指令

所有指令都需要机器人管理员权限。

1. 拉取消息 (/fetch)

拉取指定会话的历史消息，为后续的回复做准备。

指令格式: /fetch <SID> [数量|时间]

<SID>: 消息来源的唯一标识符，格式通常为 平台:消息类型:ID。例如 aiocqhttp:GroupMessage:12345678。你通常可以在机器人后台日志中找到这个值。

[数量|时间] (可选):

数量: 一个数字，代表拉取最近的多少条消息。例如 15。

时间: 数字+单位 的格式，单位可以是 h(小时), m(分钟), s(秒)。例如 1h30m。

如果此项留空，将使用配置中的 default_fetch_count。

示例:

拉取群 12345678 最近的 10 条消息:

code
Code
download
content_copy
expand_less

/fetch aiocqhttp:GroupMessage:12345678 10

拉取私聊 87654321 最近半小时内的消息:

code
Code
download
content_copy
expand_less
IGNORE_WHEN_COPYING_START
IGNORE_WHEN_COPYING_END
/fetch aiocqhttp:PrivateMessage:87654321 30m
2. 回复消息 (/reply)

对 /fetch 拉取到的消息进行回复。

指令格式 1 (手动回复): /reply <编号> <内容>

<编号>: /fetch 指令返回的消息列表中的数字编号。

<内容>: 你想要发送的回复文本。

示例:

code
Code
download
content_copy
expand_less
IGNORE_WHEN_COPYING_START
IGNORE_WHEN_COPYING_END
/reply 5 这个问题我稍后为你解答。

指令格式 2 (AI 回复): /reply <编号> LLM [额外指令]

<编号>: 同上。

LLM: 一个固定的关键词，告诉插件需要调用 AI 来生成回复。

[额外指令] (可选): 你希望 AI 在本次回复中遵守的临时要求。

示例:

让 AI 自动根据上下文回复第 5 条消息:

code
Code
download
content_copy
expand_less
IGNORE_WHEN_COPYING_START
IGNORE_WHEN_COPYING_END
/reply 5 LLM

让 AI 用非常生气的语气回复第 5 条消息:

code
Code
download
content_copy
expand_less
IGNORE_WHEN_COPYING_START
IGNORE_WHEN_COPYING_END
/reply 5 LLM 用非常生气的语气回复他，并质问他为什么这么晚才回复。
💡 使用场景

多群管理: 当你管理着数十个群，无法一一查看时，可以使用本插件快速定位并回复关键信息。

客服支持: 当机器人接到无法自动处理的用户私聊时，管理员可以远程介入，提供人工支持。

应急处理: 在机器人出现故障或被恶意刷屏时，可以快速定位问题源头并进行警告或处理。

趣味互动: 通过 /reply ... LLM 和临时指令，你可以扮演幕后导演，让机器人做出各种有趣的、符合情境的即时反应，极大增强机器人的“智能感”和趣味性。

⚠️ 注意事项

本插件的消息拉取功能依赖于特定平台适配器（如 aiocqhttp for go-cqhttp）提供的 API。如果你的机器人运行在不支持历史消息 API 的平台上，/fetch 指令可能无法使用。

请妥善保管你的管理员账号，避免指令被滥用。