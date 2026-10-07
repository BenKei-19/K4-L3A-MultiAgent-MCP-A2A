# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Hệ thống là multi-agent **deterministic** (rule-based, không gọi LLM), chạy tuần tự từng case, trong mỗi case thì payment và shipment chạy song song.

```text
inputs/<case_id>.json
        │  parse_intake (intake.py): order_id, lookup keys, claim categories
        ▼
   Coordinator ──task_assigned──► Order/item agent ──MCP──► order, item
        │ ◄──────handoff (ORDER_EVIDENCE_READY | ORDER_NOT_FOUND)
        ├──task_assigned──► Payment agent ──MCP──► payment, refund      ┐ song song
        ├──task_assigned──► Shipment agent ──MCP──► shipment            ┘
        │ ◄──────handoff (…_EVIDENCE_READY | …_MISSING)
        │  build_facts + analyze + decide (decision.py)
        ├──task_assigned──► Order/item agent ──MCP──► seller   (chỉ khi late_delivery_seller)
        ├──task_assigned──► Policy agent ──MCP──► policy ── policy_decided
        │                          └──handoff──► Verifier
        ├──task_assigned──► Verifier: build_output + verify ── verification_completed
        │ ◄──────handoff (OUTPUT_VERIFIED)
        ▼
outputs/<case_id>.json  +  traces/trace.jsonl
```

| Module | Vai trò |
| --- | --- |
| `workflow.py` | `solve_case()` và `Coordinator` |
| `agents.py` | Specialist agents, `build_facts()` |
| `a2a.py` | Envelope `A2AMessage`, `A2ABus` (assign/reply → trace) |
| `evidence.py` | `ToolRouter` (discovery → domain → arguments), `EvidenceStore` |
| `facts.py` | Chuẩn hoá payload MCP thành fact có kiểu (bảng alias Olist) |
| `decision.py` | Luật nghiệp vụ: phát hiện issue, chọn primary issue |
| `policy.py` | Đọc policy evidence, áp dụng lên finding |
| `verifier.py` | Dựng output, kiểm tra invariant, sửa tất định, validate schema |
| `mcp_gateway.py` | Client MCP: discovery có phân trang, retry có giới hạn, validate envelope |

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | `inputs/<case_id>.json` | Parse intake, giao việc, gộp fact, chọn primary issue, điều phối follow-up (seller) | `task_assigned` tới từng agent; nhận handoff; trả output cho CLI |
| Order/item | `order_ids`, lookup keys từ intake | Xác minh order tồn tại; lấy item lines; lấy seller khi được yêu cầu | Handoff `ORDER_EVIDENCE_READY`/`ORDER_NOT_FOUND` kèm order_id đã xác minh và entity id tìm thấy; `SELLER_EVIDENCE_READY` |
| Payment | order_id đã xác minh, payment_reference | Lấy payment captures và refunds | Handoff `PAYMENT_EVIDENCE_READY`/`PAYMENT_EVIDENCE_MISSING` (số payment, tổng captured) |
| Shipment | order_id, shipment_id, seller_id | Lấy timeline giao hàng (handoff carrier, delivered, estimated) | Handoff `SHIPMENT_EVIDENCE_READY`/`SHIPMENT_EVIDENCE_MISSING` |
| Policy | primary issue nháp + claim categories | Tra policy theo issue, áp dụng refund basis/rate/actions nếu có entry khớp | `policy_decided` + handoff `POLICY_APPLIED` tới verifier |
| Verifier | Finding, facts, evidence store | Dựng output, kiểm tra invariant (mục 6), sửa tất định, validate JSON Schema | `verification_completed` (`PASS`/`PASS_WITH_REPAIRS`) + handoff `OUTPUT_VERIFIED` |

**Quyền gọi tool** (theo domain do `ToolRouter` suy ra từ tên/mô tả tool khi discovery):

| Actor | Domain được phép |
| --- | --- |
| Order/item | `order`, `item`, `seller` |
| Payment | `payment`, `refund` |
| Shipment | `shipment` |
| Policy | `policy` |
| Coordinator, Verifier | không gọi tool |

Không agent nào gọi tool domain `customer` hoặc `product`, vì L3A không cần dữ liệu đó để kết luận (tránh PII và forbidden-domain).

## 3. A2A protocol

- **Envelope** (`A2AMessage`): `message_id`, `case_id`, `sender`, `recipient`, `intent`, `payload`, `evidence_refs`, `status` (`ok`/`not_found`/`missing`/`timeout`), `reply_to`.
- **Correlation:** mỗi case có một `A2ABus` riêng, gắn cố định `case_id`; mọi message và trace event của case đều mang `case_id` đó. `reply_to` nối handoff với task gốc.
- **Trace:** `assign()` emit `task_assigned` (actor = sender, target = recipient, `decision_code` = intent); `reply()` emit `handoff` kèm evidence refs của kết quả. Attributes chỉ gồm `message_id`, `reply_to`, `status`, `hop`; không trace nội dung suy luận.
- **Điều kiện handoff:** payment/shipment chỉ được giao khi order agent trả `status=ok`; seller follow-up chỉ khi primary issue là `late_delivery_seller` và có tool domain seller; policy bị bỏ qua khi `insufficient_evidence` (vẫn emit `policy_decided` với `policy_source=default`).
- **Timeout:** mỗi specialist chạy trong `asyncio.wait_for` 240 giây; hết giờ thì coordinator ghi handoff `SPECIALIST_TIMEOUT` và tiếp tục với evidence đã có.
- **Chống vòng lặp:** luồng một chiều cố định, specialist không tự giao việc cho nhau; `A2ABus` có hop budget 16 (`HopLimitExceeded`); mỗi `(tool, arguments)` chỉ gọi một lần mỗi case; tối đa 30 MCP call mỗi case; mỗi agent chạy tối đa 2 vòng thu thập.

## 4. Evidence lifecycle

1. **Discovery:** `gateway.describe_tools()` (có phân trang) → `ToolRouter` gán domain theo từ khoá trong tên (rồi đến mô tả). Không hard-code tên tool.
2. **Chọn argument:** chỉ điền từ id đã biết của case (order_id từ input sau đó được xác minh bằng evidence, các id khác lấy từ evidence). Tham số bắt buộc không điền được thì bỏ qua tool, không đoán. Tham số enum của policy được khớp với issue.
3. **Gọi và validate:** `EvidenceGateway.call()` luôn gửi `case_id` của case hiện tại, validate response theo `mcp-evidence-response-v1`, lỗi thì raise.
4. **Lưu:** `EvidenceStore` riêng cho từng case lưu `Evidence(ref, tool, domain, data, actor, args, warnings)`. Store mới mỗi case nên evidence không bao giờ dùng lại giữa các case.
5. **Emit** `tool_result_consumed` (actor = agent gọi, `tool_name`, `evidence_refs=[ref]`, attributes `domain`, số warnings) ngay khi agent nhận evidence.
6. **Map vào output:** `ISSUE_DOMAINS` xác định domain nào hỗ trợ từng issue (ví dụ `duplicate_charge` → order, payment, item; không kèm refund rỗng hay shipment). Policy evidence chỉ được trích khi case có hành động. Claim assessment chỉ trích evidence thuộc domain của claim và phải nằm trong `evidence_refs` tổng.
7. **Không bao giờ** tạo, sửa hay ghép `evidence_ref`; verifier loại mọi ref không có trong store của case.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout / transport error | Có: 1 lần retry (tổng 2 lần), backoff 0.5 giây, vì evidence read là idempotent | Đánh dấu domain thiếu; nếu thiếu order thì `insufficient_evidence`; nếu thiếu domain claim cần thì `insufficient_evidence`; còn lại confidence giảm | handoff `…_EVIDENCE_MISSING` (`failures` trong payload), `tool_failures` trong `verification_completed` |
| Not found (tool error chứa "not found"/"unknown") | Không | Order không có thì `insufficient_evidence` / `needs_investigation`, không bịa fact | handoff `ORDER_NOT_FOUND` status `not_found` |
| Source conflict (order với shipment lệch ngày; tổng order với tổng item lệch) | Không | Chọn nguồn có thẩm quyền: shipment tracking cho mốc giao hàng, item lines cho tổng tiền; ghi `data_conflicts`; confidence −0.06 | `data_conflicts[].resolution_code` = `PREFER_SHIPMENT_TRACKING` / `RECOMPUTED_FROM_ITEM_LINES` |
| Invalid MCP response (sai envelope schema, JSON lỗi) | Không | Bỏ response, không trích dẫn | Failure `INVALID_RESPONSE` → `tool_failures` |
| Invalid specialist result / timeout | Không | Coordinator tiếp tục với evidence đã có; verifier sửa tất định | handoff `SPECIALIST_TIMEOUT`; `verification_completed` = `PASS_WITH_REPAIRS` |
| Hết call budget (30/case) | Không | Dừng gọi thêm | Failure `CALL_BUDGET_EXHAUSTED` |

## 6. Verification invariants

Verifier (`verifier.verify`) kiểm tra theo thứ tự, sửa tất định và ghi mã sửa vào `verification_completed.attributes.repairs`:

1. **Evidence ownership:** mọi `evidence_refs` thuộc `EvidenceStore` của case (`EVIDENCE_OWNERSHIP`).
2. **Required anchor:** issue khác `insufficient_evidence` phải trích ít nhất một order evidence (`ORDER_EVIDENCE_REQUIRED`).
3. **Claim linkage:** ref của claim ⊆ ref của output (`CLAIM_LINKAGE`).
4. **Money totals:** amount ≥ 0 và làm tròn 2 chữ số; `recommended_refund_brl` = Σ `refund_lines` (`REFUND_TOTAL`).
5. **Status/refund/action:** `no_action` thì không refund và không action (`NO_ACTION_HAS_NO_REMEDY`); refund > 0 thì `action_required` (`REFUND_REQUIRES_ACTION`) và phải có action hoàn tiền (`REFUND_ACTION_MISSING`); `action_required` thì có ít nhất một action.
6. **Seller responsibility:** seller bị quy trách nhiệm phải nằm trong `affected_entities.seller_ids` (`SELLER_SCOPE`).
7. **Entity scope:** entity chỉ lấy từ evidence (không lấy từ lời khách), đã dedupe, tối đa 20.
8. **Confidence bounds:** trong [0, 1]; `insufficient_evidence` ≤ 0.8.
9. **Schema:** `Contracts.validate_output` (JSON Schema `l3a-output-v2`) trước khi trả output; CLI validate thêm lần nữa trước khi ghi file.

Primary issue được chọn theo nguyên tắc evidence-first, thứ tự ưu tiên: `refund_failed` > `refund_pending` > `canceled_order_paid` > `unavailable_order_paid` > `duplicate_charge` > `payment_mismatch` > `late_delivery_*`. Claim của khách chỉ dùng để phá hoà giữa các issue mà evidence đã chứng minh. Khi evidence không chỉ ra issue nào: ≥ 2 payment khớp tổng thì `valid_split_payment`, thiếu evidence cho claim thì `insufficient_evidence`, còn lại `unsupported_claim`.

## 7. Reproducibility

- **Model:** không dùng LLM; mọi quyết định tất định từ evidence nên cùng evidence cho cùng output (chỉ `event_id`/`occurred_at` của trace khác nhau).
- **Config:** `.env` gồm `COMPETITION_API_URL`, `COMPETITION_TEAM_API_KEY`, `MCP_ENDPOINT`. Không commit `.env`, không ghi key vào output/trace (validator chặn chuỗi `sk-team-…`).
- **Dependency:** theo `pyproject.toml` (Python ≥ 3.11; đã kiểm thử với Python 3.13, `mcp` 2.2.0, `httpx2` 2.13.1, `jsonschema` 4.26.0).
- **Concurrency:** các case chạy tuần tự; trong một case payment và shipment chạy song song trên cùng MCP session; mỗi lượt fan-out tối đa 5 call cho một tool.
- **Giới hạn:** 30 MCP call/case, 2 lần thử/call, 240 giây/specialist, 16 A2A hop/case.
- **Random seed:** không có; chỉ `event_id` dùng `secrets`.
- **Lệnh chạy:**

  ```bash
  day09 validate-inputs
  day09 mcp-tools                       # xem tool, domain suy ra, tham số (* = bắt buộc)
  day09 run                              # hoặc: day09 run --dump-evidence debug/evidence
  day09 validate
  day09 package --output dist/submission.zip
  pytest -q && ruff check .
  ```

- **Hiệu chỉnh khi có dữ liệu thật:** tên field → `facts.ALIASES`; từ khoá domain/tham số tool → `evidence.DOMAIN_KEYWORDS`, `evidence.PARAM_ENTITY`; từ khoá claim → `intake.CATEGORY_PATTERNS`; luật và mã → `decision.py`. Thư mục `debug/` bị gitignore và không bao giờ vào ZIP nộp bài.
