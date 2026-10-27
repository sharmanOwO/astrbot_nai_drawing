# NovelAI-Drawing

`NovelAI-Drawing` 是一个使用 NovelAI 官方图片 API 的 AstrBot 插件。它固定使用 `nai-diffusion-5-full`，支持文生图、图生图、会话参数、角色与画师预设，以及结构化提示词历史。

插件不使用中转站，所有生图请求直接发送到 NovelAI 官方图片 API。

## 安装与鉴权

此仓库同步自 2026-10-27 的运行服务器源码，插件版本保持 `1.0.0`。AstrBot 版本要求见 `metadata.yaml`（`>=4.11,<5`），声明支持 `aiocqhttp` 平台。

在 AstrBot 插件管理中使用仓库地址 `https://github.com/sharmanOwO/astrbot_nai_drawing` 安装；手动安装时，将完整源码放入 `data/plugins/astrbot_nai_drawing/`。它与 `astrbot_plugin_ppnai`（泡泡画图）是不同的插件。

仓库只包含代码和空凭据配置模板，不包含运行服务器上的 `prompt.json`、`presets.json`、用户目录、Token、个人预设和历史图片。历史文件缺失时由插件在使用过程中创建。


安装插件依赖时，应在运行 AstrBot 的同一 Python 环境中执行：

```bash
pip install -r requirements.txt
```

下面依赖安装中的 `-r requirements.txt` 表示读取依赖清单。当前清单只声明 `httpx`；其他 AstrBot 接口由宿主提供。在插件配置面板的 `model.api_key` 中填写 NovelAI Account 页面提供的 **Persistent API Token**。Token 只会作为 `Authorization: Bearer <token>` 请求头发送给：

```text
https://image.novelai.net/ai/generate-image
```

不要把 Token 写入提示词、预设文件、`prompt.json`、README 或聊天消息。

## 模型与请求格式

插件只有一个模型：`nai-diffusion-5-full`，配置面板不提供模型切换。

虽然模型是 V5 Full，官方接口仍使用结构化的 `v4_prompt` 和 `v4_negative_prompt`：

```json
{
  "model": "nai-diffusion-5-full",
  "action": "generate",
  "parameters": {
    "params_version": 4,
    "v4_prompt": {
      "caption": {
        "base_caption": "...",
        "char_captions": []
      },
      "use_coords": false,
      "use_order": false
    },
    "v4_negative_prompt": {
      "caption": {
        "base_caption": "...",
        "char_captions": []
      }
    }
  }
}
```

请求不会发送顶层 `input`，也不会发送旧版 `negative_prompt`、`ucPreset`、`qualityToggle`、`characterPrompts`、参考图数组或根级 `use_coords`。图生图使用 `action: "img2img"`，并在 `parameters` 中增加 `image`、`strength` 和 `noise`。图片会转换为不带 data URL 前缀的 Base64。

官方接口返回 ZIP 图片包。插件当前只发送其中第一张图片，因此 `n_samples` 建议保持为 `1`。

## 命令

| 命令 | 说明 |
| --- | --- |
| `/nai draw <描述>` | 用配置的 LLM 将自然语言转为英文标签后文生图 |
| `/nai tag <英文标签>` | 直接用英文标签文生图 |
| `/nai i2i <描述>` | 将自然语言转为英文标签后图生图 |
| `/nai0 <英文标签>` | `/nai tag` 的兼容别名 |
| `/nai0 i2i <英文标签>` | 直接用英文标签图生图 |
| `/nai0 positiondraw <英文标签>` | 直接用英文标签文生图，并令 `v4_prompt.use_coords` 为 `true` |
| `/nai size [尺寸]` | 查看或切换当前会话尺寸，支持 `竖`、`横`、`方`、`v`、`h`、`s` |
| `/nai nsfw [on\|off]` | 查看或切换当前会话 NSFW 负面过滤，切换权限由插件管理员控制 |
| `/naiconfig` | 显示当前会话实际生图参数和最后一次种子 |
| `/naiset <参数> [值]` | 修改或清除当前会话的生图参数覆盖 |
| `/preset ...` | 管理自己的角色或画师预设 |
| `/publicpreset ...` | 管理所有人可用的公共预设 |
| `/naiprompt ...` | 查看、保存、编辑并复用当前会话的提示词历史 |

图生图从当前消息或引用消息中选择第一张图片。自然语言命令会显式调用用于生成标签的 LLM（大语言模型）。当前源码的命令处理函数同时调用 `event.should_call_llm(True)`；是否还产生聊天回复取决于 AstrBot 的事件处理和配置，不保证命令后绝不触发聊天模型。本次仅同步源码，未改变该行为。

## 配置面板

`model` 配置组控制所有会话的默认值。`/naiset` 只覆盖当前会话，群聊中该会话就是整个群聊，私聊中就是该私聊。会话覆盖不会写回配置面板，AstrBot 重启后会恢复为面板默认值。

| 配置项 | 默认值 | 用途 |
| --- | ---: | --- |
| `api_key` | 空 | NovelAI Persistent API Token |
| `width` / `height` | `832` / `1216` | 图片宽高 |
| `steps` | `23` | 采样步数 |
| `scale` | `5.0` | CFG Scale |
| `seed` | 空 | 默认种子，留空时每次请求随机 |
| `sampler` | `k_euler_ancestral` | 采样器 |
| `noise_schedule` | `karras` | 噪声调度器 |
| `cfg_rescale` | `0.0` | CFG Rescale |
| `sm` / `sm_dyn` | `false` / `false` | SMEA 与 SMEA Dynamic |
| `n_samples` | `1` | 一次请求的生成数量，插件仅发送第一张 |
| `i2i_strength` / `i2i_noise` | `0.6` / `0.0` | 图生图强度和噪点 |
| `dynamic_thresholding` | `false` | V4 结构参数 |
| `controlnet_strength` | `1.0` | V4 结构参数 |
| `legacy` | `false` | V4 结构参数 |
| `add_original_image` | `true` | V4 结构参数 |
| `legacy_v3_extend` | `false` | V4 结构参数 |
| `skip_cfg_above_sigma` | 空 | 留空时发送 JSON `null` |
| `use_order` | `false` | 角色提示词顺序开关 |
| `custom_prompt_add` | 空 | 全局正面提示词 |
| `negative_prompt_add` | 空 | 全局负面提示词 |

`prompt_generator` 配置组用于 `/nai draw` 和 `/nai i2i` 的自然语言转标签。`provider_id` 留空时会使用当前会话的聊天模型。

`permissions.user_whitelist` 是**用户 ID** 白名单，不是群号白名单。只有名单内用户可以执行 `/naiset` 和 `/publicpreset`；空列表表示没有用户拥有这两个命令的权限。

`model.artist_presets` 是公共画师预设的面板导入来源。插件启动时会把其中条目写入本目录的 `presets.json`，其拥有者为 `public`、类型为 `artist`。

## 会话参数：`/naiset` 与 `/naiconfig`

`/naiconfig` 会显示当前会话合并后的参数。群聊中的“当前”与“上次”对所有群成员共享。

使用 `/naiset <参数> <值>` 设置覆盖值，例如：

```text
/naiset steps 30
/naiset scale 7
/naiset sampler k_euler_ancestral
/naiset use_order True
```

对于普通参数，省略值会清除该会话覆盖，后续请求重新使用配置面板的默认值：

```text
/naiset scale
/naiset sampler
```

种子有单独规则：

```text
/naiset seed 123456
/naiset seed random
```

随机模式下，插件会在每次请求前生成种子并保存为该会话的“最后一次种子”。从未生成过图时，`/naiconfig` 显示 `seed: None`。

可设置的字段包括 `width`、`height`、`steps`、`scale`、`seed`、`sampler`、`noise_schedule`、`cfg_rescale`（或 `cfg`）、`sm`、`sm_dyn`、`n_samples`、`i2i_strength`、`i2i_noise`、`dynamic_thresholding`、`controlnet_strength`、`legacy`、`add_original_image`、`legacy_v3_extend`、`skip_cfg_above_sigma` 和 `use_order`。

## 角色与画师预设

预设保存于插件目录的 `presets.json`。角色保持功能目前不参与请求，但预留目录为：

```text
presets.json
cs/
  <用户 ID>/
    <角色保持名称>.txt
```

预设记录使用复合键避免不同用户或不同类型的同名冲突，记录内容为：

```json
{
  "presets": {
    "12345::character::猫娘": {
      "title": "猫娘",
      "content": "tag=girl, cat ears, tail, cute",
      "owner_id": "12345",
      "type": "character"
    },
    "public::artist::channel": {
      "title": "channel",
      "content": "1.0::channel_(caststation) ::",
      "owner_id": "public",
      "type": "artist"
    }
  }
}
```

创建或覆盖个人预设：

```text
/preset character 猫娘 = girl, cat ears, cute, smile, tail
/preset c 猫娘 = girl, cat ears, cute, smile, tail
/preset artist channel = 1.0::channel_(caststation) ::
/preset a channel = 1.0::channel_(caststation) ::
```

创建或覆盖公共预设：

```text
/publicpreset character miku = girl, hatsune miku, turquoise hair, twintails
/publicpreset a channel = 1.0::channel_(caststation) ::
```

省略等号和内容时列出对应类型：`/preset character`、`/preset c`、`/preset artist`、`/preset a`、`/publicpreset character`、`/publicpreset artist`。查询或引用时，个人预设优先于同名公共预设。名称匹配忽略大小写、空格和换行。

在任意用户提示词中写 `character = 名称` 或 `c = 名称` 会直接展开角色提示词；写 `artist = 名称` 或 `a = 名称` 会提取画师提示词。找不到的名称会保留原文本，方便检查拼写。

## V4 结构化提示词

正面提示词的实际顺序固定为：

```text
全局正面提示词 → 画师预设 → 用户提示词
```

例如全局提示词为 `masterpiece, best quality`，画师为 `1.0::channel_(caststation) ::`，用户输入：

```text
3girls, indoors, mahjong table | girl, Hatsune Miku, turquoise twintails | girl, Kasane Teto, red drill hair
```

请求中的正面结构为：

```json
{
  "caption": {
    "base_caption": "masterpiece, best quality, 1.0::channel_(caststation) ::, 3girls, indoors, mahjong table",
    "char_captions": [
      {
        "char_caption": "girl, Hatsune Miku, turquoise twintails",
        "centers": [{"x": 0.2, "y": 0.5}]
      },
      {
        "char_caption": "girl, Kasane Teto, red drill hair",
        "centers": [{"x": 0.8, "y": 0.5}]
      }
    ]
  },
  "use_coords": false,
  "use_order": false
}
```

第一个 `|` 之前的内容写入 `base_caption`，后续每一段依次写入 `char_captions`。无论 `use_coords` 是 `true` 还是 `false`，每个角色都必须有 `centers`，未手动指定时由插件生成随机小数坐标。`/nai0 positiondraw` 仅将 `use_coords` 设为 `true`；`/naiset use_order True` 控制 `use_order`。

全局负面提示词与 NSFW 过滤标签写入 `v4_negative_prompt.caption.base_caption`。

## 提示词历史：`prompt.json` 与 `/naiprompt`

每次生成后，插件会在目录内 `prompt.json` 追加一条记录。每个会话最多保留 20 条 `temporary` 记录，最早的临时记录会被删除；标记为 `permanent` 的记录不受此限制。

```json
[
  {
    "user_id": "12345",
    "group_id": "67890",
    "request_timestamp": "0905123456789",
    "response_time": 3.42,
    "type": "temporary",
    "prompt": {
      "global_prompt": "masterpiece, best quality",
      "artist": "1.0::channel_(caststation) ::",
      "base_caption": "3girls, indoors",
      "char_captions": [
        {
          "char_caption": "girl, turquoise hair",
          "centers": [{"x": 0.2, "y": 0.5}]
        }
      ]
    },
    "negative_prompt": {
      "global_negative_prompt": "",
      "base_caption": "",
      "char_captions": []
    }
  }
]
```

`group_id` 在私聊中为 `null`，在群聊中为群号。`request_timestamp` 使用北京时间的月日时分秒毫秒，`response_time` 是从发送官方 API 请求到接收响应的秒数。

常用查询与保存：

```text
/naiprompt l
/naiprompt last 3
/naiprompt save 2
```

`l` 与 `last` 默认查询上一次记录，后面数字表示向前第几次。`save` 会将对应记录变为 `permanent`。

用时间戳或序号复用一条提示词并立即生图：

```text
/naiprompt 0905123456789
/naiprompt 1
```

可以在复用时通过点号路径修改内容。`=` 覆写，`+` 在末尾追加逗号标签，`-` 按忽略大小写、空格和换行的方式删除匹配标签：

```text
/naiprompt 1 prompt.artist = 1.0::channel_(caststation) ::
/naiprompt 1 prompt.char_captions[1] + mutual#eye contact
/naiprompt 1 prompt.char_captions[1].char_caption - mutual#eye contact
/naiprompt 1 prompt.char_captions[0] = character = miku
/naiprompt 1 prompt.char_captions[0].centers = (0.7,0.5)
```

`prompt.char_captions[n]` 是 `prompt.char_captions[n].char_caption` 的简写，也接受 `charcaptions` 这一写法。坐标可以写 JSON 数组、`(x,y)` 或 `(x=...,y=...)`。多条修改用反斜杠 `\` 分隔：

```text
/naiprompt 1 \prompt.char_captions[0] = c = miku \prompt.char_captions[1] + source#pointing \prompt.char_captions[1].centers = (0.7,0.5)
```

## 其他配置

- `nsfw_filter`：全局默认 NSFW 过滤开关和注入到负面提示词的标签
- `admin`：控制管理员模式及 `/nai nsfw` 的管理员判定
- `auto_recall`：图片发送后的自动撤回设置
- `prompt_show`：是否在生成前发送最终英文标签
- `custom_prompt.system_prompt`：附加到自然语言转标签模板的系统提示词


## 快速验证与常见问题

安装后先填写自己的 `model.api_key`，再发送 `/nai help` 查看命令，用 `/nai tag landscape, blue sky` 验证直接标签生图。`/nai draw` 还需要可用的聊天模型来转换标签。实际生图会使用账户额度，本仓库同步过程没有调用付费接口。

- API 鉴权失败：核对自己的 Persistent API Token，不要提交或公开 Token。
- `/naiset` 或公共预设提示无权限：检查 `permissions.user_whitelist`，这里填写用户 ID；默认空名单不放行。
- 预设、历史写入失败：确保 AstrBot 对插件目录有写权限。
- 生图成功但发送失败：分别核对官方 API 请求与消息平台发送阶段，避免未经确认重复生成。

本插件主要以命令提供功能，当前入口未注册 `@filter.llm_tool` 生图工具。不要将其他生图插件的工具名套用到这里。

源码目录中的 `.gitignore` 排除了运行历史、预设、角色数据、缓存、Cookie、密钥及本地配置。新增配置应保存在 AstrBot 配置面板或私有运行环境，不应填写到 `_conf_schema.json` 的默认值中。
