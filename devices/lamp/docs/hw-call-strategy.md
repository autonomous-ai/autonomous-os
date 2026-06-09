# HW Call Strategy: Inline Markers vs Batch API

## Current: Inline Markers (SKILL.md dạy LLM viết `[HW:...]`)

```
LLM nghĩ 1 lần → output: [HW:/emotion:...][HW:/servo:...] Reply text → lifecycle end → Lamp parse → fire HW
```

- **1 inference round**
- HW fire **chậm** (đợi turn end mới parse)
- Thủ công: phụ thuộc LLM viết đúng format `[HW:...]`
- OpenClaw không biết gì về markers — chỉ là text
- LLM đôi khi "lạc" dùng exec/curl thay vì marker (inconsistent)

## Current: Exec Tool (LLM tự gọi curl)

```
LLM nghĩ → tool_call(exec, curl /emotion) → đợi result → nghĩ → tool_call(exec, curl /servo) → đợi result → nghĩ → text
```

- **N+1 inference rounds** (mỗi tool = +1 round)
- HW fire **ngay** khi tool execute
- Tốn token, chậm tổng thời gian turn
- LLM thấy được result (nhưng HW calls không cần)

## Proposed: Batch API trên Lamp

### Concept

Tạo 1 endpoint duy nhất, LLM chỉ truyền key nào cần call:

```json
// Chỉ emotion
{"emotion": {"emotion": "happy", "intensity": 0.9}}

// Emotion + servo
{"emotion": {"emotion": "curious", "intensity": 0.8}, "servo": {"recording": "scanning"}}

// Chỉ LED
{"led": {"color": [255, 0, 0]}}

// Full combo
{"emotion": {...}, "servo": {...}, "led": {...}, "scene": {...}}
```

```
POST http://127.0.0.1:5001/hw/batch
```

Lamp nhận → check key nào có → chỉ dispatch những cái đó, song song → return 1 response.

### Flow

```
LLM nghĩ → 1 tool_call(exec, curl /hw/batch) → Lamp fire ALL song song → result "ok" → LLM nghĩ → text reply
```

### So sánh

| | Inline Markers | Exec Sequential | **Batch API** |
|---|---|---|---|
| Inference rounds | **1** | N+1 | **2** |
| HW fire timing | Chậm (đợi end) | Ngay nhưng tuần tự | **Ngay + song song** |
| LLM format dependency | Cao (phải viết [HW:...]) | Thấp | **Thấp** |
| Token cost | Thấp nhất | Cao | **Thấp** (round 2 nhẹ) |
| Consistency | LLM hay lạc format | OK | **Tốt** (1 tool, 1 schema) |
| LLM thấy result | Không | Có | Có |

### Ưu điểm

1. **Nhanh** — 1 tool call, tất cả HW fire song song, không tuần tự
2. **Consistent** — LLM chỉ cần biết 1 tool với JSON schema rõ ràng
3. **Đơn giản** — 1 SKILL.md thay vì nhiều cái dạy inline format
4. **Reliable** — tool thật, không phụ thuộc LLM viết đúng regex pattern

### Tại sao không dùng `parallelToolCalls`?

OpenClaw hỗ trợ param `parallel_tool_calls` nhưng:
- **Chỉ work với OpenAI-compatible APIs** (openai-completions, azure-openai-responses)
- **Claude/Anthropic API không hỗ trợ param này** — Claude tự quyết định emit parallel hay không
- Project đang dùng `claude-haiku-4-5` qua Anthropic API → param này **không có tác dụng**
- Ngay cả khi work, OpenClaw vẫn xử lý tool results **sequential** (không concurrent) và mỗi result **phải trả về LLM** — không có fire-and-forget

→ Không thể dùng native parallel tool calls để giải quyết vấn đề này. Batch API là workaround: gom N tools thành 1 tool call duy nhất.

### Nhược điểm

1. 2 inference rounds thay vì 1 (nhưng round 2 rất nhẹ)
2. Cần tạo `/hw/batch` endpoint trên HAL (Python side)

### Implementation cần làm

1. **HAL Python**: Tạo `POST /hw/batch` endpoint, nhận JSON, dispatch song song tới các service
2. **SKILL.md**: Viết 1 skill mới `hw-batch` thay thế emotion/servo/led/scene skills
3. **Lamp Go handler**: Có thể giữ nguyên (exec tool path đã hoạt động), hoặc thêm detect `/hw/batch` trong tool args để log chi tiết hơn
