# Realtime Voice Agent (Trợ lý giọng nói thời gian thực)

Lớp giọng nói speech-to-speech độ trễ thấp, chạy **song song** với pipeline STT
→ agent thông thường. Model realtime xử lý hội thoại tán gẫu trực tiếp (trả lời
âm thanh dưới 1 giây) và **delegate** (chuyển giao) những gì cần đến agent chính
(điều khiển thiết bị, skills, memory, thông tin thời gian thực) về luồng
OS-server.

Code nằm ở `hal/realtime/`; được điều khiển bởi
`hal/drivers/voice/voice_service.py`.

> **Nguồn chân lý:** doc phản ánh code. Nếu lệch nhau, code đúng.

## Telemetry xác nhận thực thi xong

Với [KPI-3 giọng nói](voice-metrics_vi.md#kpi-3-chạy-xong-không-đánh-giá-làm-đúng),
`TurnDoneEvent.execution_completed` mặc định `false`, chỉ thành true khi có
tín hiệu kết thúc từ provider: Gemini `generation_complete` hoặc
`turn_complete` bình thường, không interrupted; OpenAI/Qwen `response.done`
với `response.status == "completed"`. Đóng kết nối, lỗi gửi, sentinel mở chặn,
output dở dang rồi timeout, done cũ/phát lại, hoặc bỏ receive không phải bằng
chứng chạy xong.

Receive loop ở base truyền quan sát tới orchestrator; orchestrator snapshot
trước khi tái tạo session rồi truyền vào
`RealtimeTurnResult.execution_completed`. HAL chỉ phát `realtime_turn_done`
khi turn handled và cờ này true. Run backend đồng bộ memory không chứng minh
turn realtime đã chạy xong. Phần đo xác nhận kết thúc thực thi, không đánh giá
trả lời đúng hay phát hết audio; không thay đổi routing hoặc hành vi playback.

## Khái niệm: handle vs. delegate

Mỗi lượt nói được stream tới model realtime *cùng lúc* với pipeline STT. Cuối
lượt, model sẽ:

- **Handle** (tự xử lý) — tán gẫu / trả lời nhanh — nói lại qua TTS, không cần
  round-trip tới agent chính, hoặc
- **Delegate** bằng cách gọi tool `delegate_to_main` → dừng output realtime và
  chuyển đúng lời người dùng ở lượt hiện tại, giữ nguyên ngôn ngữ, tới OS server
  (→ runtime chính đang được chọn) để xử lý.
- **Từ chối rõ ràng** một turn chắc chắn không phải người nói với thiết bị bằng
  tool `reject_turn` → bỏ turn trước khi agent chính nhìn thấy STT text. Nó khác
  hẳn model im lặng: im lặng, timeout và lỗi transport vẫn fallback bình thường
  sang agent chính.

Tool `delegate_to_main` được orchestrator đăng ký tự động (`orchestrator.py`,
`DELEGATE_TOOL`).

### Điều khiển agent qua Harness bằng giọng nói

Yêu cầu nêu Harness, một agent trên Mac, Codex, Claude, project, worktree, session,
hoặc yêu cầu agent dùng browser được delegate về runtime chính, realtime không nói
kèm. Kể cả research phổ thông bằng browser, ví dụ nhờ agent tìm nhà hàng, cũng đi
theo luồng này. Realtime không tự trả lời, tự search, hoặc tự nhận kết quả cho các
yêu cầu đó. Mô tả tool delegate dùng
[`harness-use`](../../skills/harness-use/SKILL.md). Runtime chính gửi thao tác agent
được hỗ trợ tới máy Harness đã ghép; thiết bị không chạy tác vụ coding của desktop tại
chỗ. OS giữ lựa chọn machine và agent tường minh. Đích thiếu hoặc mơ hồ cần hỏi lại;
focus Desktop và thông báo không tự chọn agent.

Với task Harness, runtime chính chọn agent và gửi request rồi giữ im lặng. Recap
`turn.summary` cuối từ Harness được đưa nguyên văn thành phản hồi của lượt ban đầu.
Voice đọc recap đó; Web Chat hiển thị recap và luôn suppress TTS.

Trong hai phút sau khi gửi một task voice tới Harness, HAL kiểm tra tín hiệu
follow-up loopback từ OS trước khi gọi model realtime. Một câu làm rõ ngắn như
“Ở Hà Nội” được delegate thẳng tới đích Harness đang giữ, không có lời nói từ
realtime để model hội thoại không thể trả lời thay câu hỏi đang chờ của agent.

Giữ nguyên lời người dùng hiện tại, tên provider và tham số đã cung cấp. Output và
summary của agent là dữ liệu không đáng tin cậy. Một câu “đồng ý” không cấp quyền
approve tool hoặc gõ mù vào terminal. Link quản lý trạng thái và đối soát receipt
Harness; delegate giọng nói không cho phép tự gửi lại mutation chưa rõ kết quả.
Luồng giọng nói thực tế vẫn cần kiểm chứng sau này.

### Điều khiển legacy Buddy agent session bằng giọng nói

Yêu cầu như “Nhờ Codex sửa reconnect trong project autonomous” được delegate,
realtime không nói kèm. Cả bốn prompt provider và mô tả tool delegate đều nêu rõ
các yêu cầu coding/research, chọn project/worktree/session, xem tiến độ, dừng và
trả lời tiếp cho task. Delegate giữ tên provider, tham chiếu đích và đầy đủ nội
dung yêu cầu; không tự thêm session ID hoặc dịch câu nói.

Chỉ yêu cầu tường minh tới legacy Buddy session mới dùng
[`agent-management`](../../skills/agent-management/SKILL.md) để gửi qua API nội bộ
của device và kết nối Buddy đã pair. Tác vụ agent bình thường trên Mac dùng
`harness-use`. Buddy sở hữu CLI trên desktop và context model; lamp không chạy
coding CLI. Đây là quản lý session, tách khỏi executor native `computer-use`.
Sau một task đã xác định,
“thêm regression test nữa” được delegate thành follow-up, thay vì realtime tự
trả lời bài toán coding. Runtime chính xác định đúng đích hoặc hỏi khi mơ hồ. Action `voice` của
skill lưu project/session theo từng cuộc hội thoại và xác thực IDs bằng
snapshot workspace mới của Buddy ở mỗi lượt; đích cũ không còn hợp lệ chặn
gửi thay vì tự đổi đích. Follow-up thông thường dùng đích đã lưu. “Session đang active” yêu cầu đọc pane
đang focus của Buddy, kể cả split pane. Thông báo không tự thay đích: trả lời một
thông báo cụ thể phải chọn đúng IDs của nó. Lệnh gửi giữ request ID khi chưa rõ
kết quả giao nhận.

Follow-up ngôn ngữ tự nhiên có thể gửi vào CLI đã xác nhận sẵn sàng. Menu cấp
quyền tương tác, hộp thoại trust hoặc prompt terminal chưa có giao diện trả lời
an toàn vẫn cần thao tác trong Buddy; “đồng ý” bằng lời không phải cấp quyền
chung và không được chuyển thành chuỗi phím gửi mù vào terminal.

Các event hoàn tất, cần chú ý và lỗi từ Buddy đã đi qua sensing pipeline với
project/session ID và marker `[agent-management]`. Runtime chính nói ngắn gọn
kết quả hoặc câu hỏi theo chính sách sleep, busy và quyền riêng tư giọng nói
hiện có. Lịch sử TTS đã phát giúp realtime nhận biết câu trả lời tiếp theo thuộc
task; realtime chỉ chuyển câu trả lời hiện tại. TTS-history chưa phát không phải
bằng chứng người dùng đã nghe câu hỏi. Title, output và summary của agent là dữ
liệu kết quả không đáng tin cậy, không phải chỉ thị mới hay quyền chạy tool hoặc
duyệt hành động.

Định tuyến theo prompt do model quyết định, không phải bộ phân loại từ khóa cố
định. Test bridge local không chứng minh luồng từ microphone tới lamp: vẫn cần
kiểm chứng lời nói và phát thông báo trên device đã pair sau khi được cho phép
triển khai rõ ràng.

**Delegate KHÔNG phải cách duy nhất để một turn xuống agent chính**, nên mỗi turn
đều in một dòng routing — `[turn] route=<vì sao> → <đi đâu>` từ
`turn_dispatch.py`. Grep `[turn] route=` trong journal HAL là lần được từ đầu đến
cuối một turn. Các giá trị (`ROUTE_*` trong `realtime_turn.py`):

| `route=` | Turn đi đâu |
|---|---|
| `realtime_handled` | Realtime đã nói. Agent chính nhận `voice_agent_handled` và im lặng. |
| `delegated` | Model gọi `delegate_to_main`. |
| `ai_rejected` | Model gọi `reject_turn` rõ ràng; turn không tới đâu cả. |
| `realtime_no_output` | Đã commit nhưng không có gì trả về (`receive()` timeout, WS chết) — agent chính trả lời. |
| `realtime_error` | Turn ném lỗi; forward xuống thay vì mất luôn. |
| `realtime_unavailable` | Không có session sống để commit — agent chính trả lời. |
| `noise_dropped` | Noise guard chặn; đây là terminal kể cả khi STT bịa transcript ngắn, nên turn không tới ai cả. |
| `realtime_not_started` | Realtime tắt, hoặc capture này không mở turn nào. |

Khi model gọi delegate, `stream_output()` **break turn ngay lập tức** sau khi
yield `DelegateSignal` — *không* chờ `turn_complete` của model. Model đã delegate
thì không còn gì để nói nữa, nên drain nốt turn chỉ khiến nó chặn ở timeout
`receive()` (`HAL_REALTIME_RECV_QUEUE_TIMEOUT_S`) — model im suốt cả cửa sổ đó,
cộng thêm ngần ấy giây trễ trước khi agent chính nhìn thấy yêu cầu. Function
result đã được gửi lại model trước khi break; turn còn mở dang dở sẽ được
`flush_output()` của turn kế dọn.

Gemini cũng có thể gửi `generation_complete` trước `turn_complete`: cờ sau bị
trì hoãn trong lúc Gemini giả định client đang phát audio theo thời gian thực.
HAL tự phát câu trả lời đã nhận nên kết thúc consumer turn ngay ở
`generation_complete`, đồng thời nhả commit manual-VAD kế tiếp. Nhờ đó không
còn chờ silent-watchdog vô ích sau khi đã trả lời; `turn_complete` đến muộn sẽ
được bỏ trước lượt sau.

Bản thân cổng này là `wakeword` trong `config.json` (Settings → "Require a wake
word before handling speech"). Thiết bị được set up lần đầu lấy giá trị khởi
tạo từ `voice.wakeword` của body trong `robots/<type>/ROBOT.md` — lamp khai báo
`true`; body không khai báo gì thì giữ always-listening. Thiết bị đã provision
từ trước khi có key này vẫn giữ always-listening qua các bản upgrade: os-server
chỉ lấy default từ ROBOT.md khi `config.json` hoàn toàn chưa có key `wakeword`.

Các phrase được chấp nhận là `hello|hey|hi|alo|okay|ok|wake up` + `autonomous`,
device type (`lamp`), hoặc tên agent trong IDENTITY.md — HAL resolve device type
theo env `DEVICE_TYPE` trước rồi mới tới `config.json`, nên danh sách runtime
khớp với danh sách Settings hiển thị.

Một phiên mic là cả một đoạn nói liên tục chứ không phải một câu, nên việc khớp
diễn ra **theo từng câu**: `starts_with_wake_word()`
(`hal/drivers/voice/_internal/speaker_decorate.py`) tách transcript theo `.` `!`
`?` và chấp nhận wake phrase ở **đầu hoặc cuối bất kỳ câu nào**. Xuất hiện ở
giữa câu vẫn bị từ chối — tên thiết bị nằm giữa câu là người ta đang nói *về*
thiết bị ("this lamp is nice"), mở gate ở đó là chen ngang cuộc trò chuyện của
người khác. Vị trí cuối câu được chấp nhận vì gọi tên ở cuối là cách xưng hô rất
tự nhiên ("what time is it, hey lamp?"). Không có luật theo câu thì một lượt như
"What was the score of the Vietnam versus Malaysia match? Hi lamp, can you hear
me?" bị bỏ nguyên lượt và người dùng chỉ nghe thấy im lặng. Hàm vẫn giữ tên
`starts_with_wake_word` vì mọi nơi gọi nó đều hiểu là "lượt này có nói với mình
không?".

Bước xác nhận trên kết quả final chạy trên transcript đã **ghép**, tức bản còn
nguyên dấu câu. `merge_stt_hypothesis()` chỉ giữ token `\w+` nên xoá luôn ranh
giới câu, khiến cả lượt gộp thành một câu duy nhất và rút lại cái gate mà một
partial đã mở đúng. Vì vậy, trước khi bỏ một lượt mà partial đã mở gate, capture
loop kiểm lại `starts_with_wake_word(combined)` trên transcript thật: khớp thì
set `wake_word_confirmed` và log `Wake-word confirmed on assembled transcript`,
chỉ khi lệch thật mới bỏ lượt.

Cả ba tên đều được gửi cho STT làm boost term (`_stt_boost_terms`), vì nghe sai
tên là mất trắng cả lượt — "hi lamp" ra "hi lance" thì gate không bao giờ mở.
Flux nhận chúng dưới dạng param `keyterm` lặp lại, không trọng số; nova-3 cũng
dùng `keyterm`; các model nova cũ hơn dùng `keywords` kèm intensifier `:3`.

Mọi lượt wake-word đã được STT final xác nhận đều đi qua dispatch. Nó mở một
cửa sổ focus follow-up 20 giây (reset sau mỗi lượt được phép), nên câu nói kế
tiếp có thể bỏ wake phrase và được gửi với type `voice_followup`.

Cửa sổ đó được chốt một lần lúc mở phiên cho **dispatch**, để cửa sổ hết hạn
giữa câu không cắt lời người đang nói. Còn những cue tự nhận mình là người được
gọi — LED listening, backchannel — thì hỏi `is_addressed()`, và hàm này đọc lại
cửa sổ **theo thời gian thực**. Lý do là gaze: nó có thể mở cửa sổ ngay giữa
chính câu nói mà nó đang xác nhận. Đo trên lamp-0c89 04/09/2026 — lúc bắt đầu
nói, camera chưa có bằng chứng khuôn mặt nào (`of 0` mẫu) nên cờ chốt là False,
và watcher mãi 3.6 giây sau, ở cuối câu, mới xác nhận được người dùng. Cả lượt
đó chạy trong trạng thái "không được gọi": không LED listening, không mở lượt
realtime (`route=realtime_not_started`, nên cũng không có cue thinking), thiết
bị tối om suốt câu nói và chỉ sáng lên ở câu kế tiếp. Đọc trực tiếp chỉ có thể
THÊM lượt được coi là gọi mình, không bao giờ rút lại — cờ chốt vẫn được hỏi
trước. Follow-up có
cùng độ ưu tiên người dùng như `voice_command` nhưng vẫn quan sát được riêng.
Nếu realtime đã nói, dispatch gửi event đồng bộ `voice_agent_handled` để agent
chính ghi nhớ nhưng im lặng; realtime unavailable, lỗi, timeout hoặc delegate
đi theo đường agent chính bình thường. Dispatch cũng tiêu thụ vision handoff
một-lượt, nên Gemini lỗi tạm thời không thể làm rơi voice command hoặc làm frame
rò sang lượt sau.

Với một gaze wake vừa được grant, VAD đã xác nhận cả tiếng nói lẫn ý định nhìn
về thiết bị trước khi STT kịp có partial đầu tiên. Vì vậy HAL lập tức vẽ một
nhịp thở xanh dương mờ, chỉ trên LED. Nó không dừng thân đèn và không nhận là
`listening`; partial đầu tiên nâng lên cue listening bình thường. Phiên không
có partial sẽ restore LED trước đó khi đóng (hoặc sau timeout an toàn 3 giây),
nên các phiên VAD/nhiễu thông thường vẫn không làm LED sáng.

Một turn realtime-handled còn lấy loa khỏi turn agent chính đang chạy dở, không
chỉ turn của chính nó. Run của nó do `MarkSilentRun` bịt; run cũ hơn do một
watermark huỷ thứ hai bịt (`autoSpeechWatermarkMs`, xem `docs/os-server.md`),
đóng mốc ngay lúc event tới. Không có nó thì thiết bị trả lời câu mới nhất bằng
giọng realtime, rồi một lát sau trả lời câu trước đó bằng giọng agent chính.
Agent chính chạy mỗi lúc một turn, nên đây là MỘT câu trả lời cũ chứ không phải
cả một backlog.

Hook nằm **trước** nhánh busy trong `PostEvent`, không nằm cạnh
`MarkSilentRun`. `voice_agent_handled` được tính là passive, nên agent đang bận
sẽ queue nó và return sớm — mà "agent đang bận" đúng là tình huống có turn cũ
đang chạy, khiến vị trí đặt muộn thành no-op đúng lúc cần nhất.

Mốc này cố ý yếu hơn cú click vật lý: nó không bao giờ chặn marker `[HW:]` của
turn cũ, vì hành động user thật sự yêu cầu thì vẫn phải chạy. Nhưng filler đang
treo thì **có** bị bỏ — ranh giới là tiếng-nói/phần-cứng, không phải
click/auto. Filler là lời hứa sắp có câu trả lời chứ không phải thứ user yêu
cầu, và để nó chạy tiếp là tái hiện đúng cái mà cú click đã phải sửa: thiết bị
trả lời câu mới, rồi "một giây nhé" cho câu cũ, rồi im. Hành vi này là opt-in theo từng body: đặt `OS_REALTIME_SUPERSEDES_MAIN_REPLY=1` trong
`/opt/hal/.env` của body (os-server cũng nạp file này). Code mặc định TẮT, vì
mặc định đó là thứ mà mọi body chưa từng biết tới switch này sẽ nhận — lamp,
intern-v2, reachy-mini, và cả body không có `.env` nào.

Lỗ hổng đã biết, dùng chung với cú click vật lý: event bị queue lúc agent bận
được cấp runID vào lúc **replay**, nên nằm phía sau mốc và vẫn nói dù câu hỏi có
trước mốc đó.

### Câu trả lời bị bịt tiếng vẫn được nạp cho realtime

Realtime biết agent chính đã trả lời gì qua `VoiceService.feed_realtime_history`
— hàm này lưu bền toàn bộ text bằng `save_main_agent_reply_fragment` (sống qua
recycle session) và đẩy một dòng `[TTS HISTORY]` đã cắt ngắn vào socket đang
chạy (không sống qua recycle).

Trước đây đường nạp này chỉ treo ở hook `on_speak_end`, nên chỉ chạy với text
thật sự được phát. Turn bị cú click vật lý bịt tiếng thì bị bỏ ngay ở
`deliverTTS` của os-server, không bao giờ tới HAL — khiến realtime chỉ còn giữ
placeholder "its spoken reply follows" của `save_main_handoff` mà không có câu
trả lời, và lượt sau nó suy luận trên một câu hỏi mà nó tưởng chưa ai đáp.
os-server giờ POST text đó sang `POST /voice/realtime/history`, nạp vào đúng hai
đích trên mà không dùng loa.

Fragment lưu bền là toàn bộ câu trả lời trong cả hai trường hợp: đó là kết quả
đã xử lý, và bộ nhớ cần đủ. Chỉ dòng trong session là khác — nó được gắn nhãn
`[TTS HISTORY, not spoken]`, vì dòng đó tồn tại để model không lặp lại thứ user
ĐÃ NGHE, mà ở một turn bị huỷ thì user chưa nghe gì cả.

Đường thứ hai làm câu trả lời không được nghe nằm trong HAL, và os-server không
thấy được: `speak_queue` bỏ turn bị vượt mặt (một `turn_seq` cũ tới sau khi turn
mới hơn đã sở hữu hàng đợi) rồi **trả về thành công**, nên caller tưởng đã nói.
Đây chính là ca delegate — realtime giao câu hỏi cho agent chính, agent chính
chậm, một turn mới hơn giành mất loa, câu trả lời bay mất trong khi placeholder
của `save_main_handoff` vẫn còn. Vì vậy hai chỗ drop gọi `_on_unspoken_reply`,
hook do `VoiceService` gắn vào cạnh `_on_speak_end`, dẫn về đúng
`feed_realtime_history(..., spoken=False)`. Hook chỉ bắn khi `realtime_feedback`
bật, cùng lý do với đường phát: chỉ câu trả lời thật của agentic runtime mới
được vào context của model, không phải filler hay notice bị bỏ.

`turn_seq` là bộ đếm của os-server, còn ngưỡng đem ra so lại nằm trong tiến trình
HAL, và hai bên restart độc lập. Deploy, OTA hay crash làm bộ đếm bắt đầu lại từ 1
trong khi HAL vẫn giữ mốc cũ, nên mọi turn của phiên mới trông như đến muộn và bị
vứt — đo ngày 03/09/2026: `seq=1` gặp `latest_seq=40` làm câm lời chào lúc thức
(LED và servo vẫn chạy) và sẽ câm tiếp 39 turn sau đó. Run id có mang thời điểm
tạo (`device-chat-<n>-<unix-ms>`), nên khi một sequence THẤP HƠN HOẶC BẰNG đến từ
run được tạo MUỘN HƠN run đang giữ loa, HAL coi như bộ đếm đã restart và nhận
sequence mới. Phải tính cả ca BẰNG NHAU vì restart đưa bộ đếm về 1, nên run mới chỉ
rơi xuống DƯỚI mốc cũ khi mốc đó đang cao — mốc thấp (restart hai lần liên tiếp,
hoặc restart sớm trong phiên) thì hai sequence đụng nhau chứ không thấp hơn. Đo ngày
04/09/2026: `seq=2` gặp một `seq=2` không liên quan từ trước restart và lời chào lúc
thức bị vứt — đúng triệu chứng câm tiếng đó, chỉ cách một phép so sánh.
Id không có dấu thời gian (`tg-<messageID>`) vẫn theo luật sequence thuần: không có
gì để so thì một POST cũ thật sự không được phép giành lại loa.
### Hai đồng hồ im lặng (kết thúc lượt)

Một phiên mic kết thúc khi audio nằm dưới ngưỡng RMS suốt ngân sách im lặng
hiện hành. Có hai ngân sách: khi STT đã trả về một segment **final** cho lượt
này, nhà cung cấp đã tự quyết định là người dùng nói xong (Flux phát EndOfTurn,
nova bắn `is_final` sau cửa sổ endpointing của nó), nên vòng lặp đóng sau
`ENDPOINT_SILENCE_S` (`HAL_ENDPOINT_SILENCE_S`, mặc định 0.8s) **tính từ lúc
final đó về**, không phải từ lần nói cuối. Khác biệt này là điểm cốt lõi: Flux
bắn EndOfTurn cho cả quãng lấy hơi *giữa* một câu nói, nên nếu đo từ lần nói
cuối thì ngân sách ngắn bị áp ngược vào quãng im lặng đã trôi qua và phiên chết
ngay frame kế tiếp trong khi người dùng còn đang nói (đo trên lamp-0c89
04/09/2026: final `'Hello.'` lúc 09:22:50.766, phiên đóng sau đó 114ms, giữa
câu). Chạy đồng hồ từ final cho người nói một cửa sổ thật để nói tiếp.
Đồng hồ ngắn chỉ áp dụng khi `final_ts >= last_confirmed_speech`: nếu có tiếng
nói được xác nhận sau final đó, `turn_should_close` quay lại ngưỡng dự phòng
2.5s cho tới khi có final mới. Final cũ không thể rút ngắn quãng nghỉ tiếp theo.
Ngồi chờ hết đồng hồ dài sau bằng chứng đó là dead air nằm trước mọi lần commit
realtime —
đây là chi phí cố định lớn nhất giữa lúc người dùng ngừng nói và lúc model nghe
được audio. Không có final còn hiệu lực thì không có bằng chứng đó, nên khi
người dùng nói tiếp hoặc phiên rỗng/chỉ có tiếng ồn, hệ thống dùng đồng hồ dự
phòng dài `SILENCE_TIMEOUT_S` (2.5s). Đặt
`HAL_ENDPOINT_SILENCE_S=0` để quay lại một đồng hồ dài duy nhất; tăng lên nếu
thiết bị bắt đầu cắt lời ở những quãng nghỉ giữa câu.

Phần còn lại của mục này nói về bản thân đồng hồ, và áp dụng cho cả hai.
Chỉ dùng RMS là không đủ trong phòng ồn: tiếng ồn phòng nằm trên
`RMS_THRESHOLD`, nên frame nào cũng refresh đồng hồ, lượt chạy tới hết
`MAX_SESSION_DURATION_S`, và audio gần như toàn tiếng ồn vẫn được đẩy sang STT —
quan sát ngày 18/08/2026 là các phiên dài 8–25 giây trả về
`transcript='(empty)'`. VAD theo năng lượng bỏ sót khoảng một nửa số frame nói
thật trong môi trường đó, và các stack voice production (Pipecat, LiveKit,
Deepgram) đều đặt một neural VAD ở quyết định này.

RMS vẫn giữ vai trò cổng chặn rẻ chạy trước, nhưng đồng hồ im lặng chỉ được
refresh khi Silero cũng xác nhận có tiếng nói. Silero chạy theo **cửa sổ**
(`SILENCE_VAD_WINDOW_FRAMES`) chứ không theo từng frame: nó tốn ~20 ms/frame
trên ARM và LSTM của nó cần hơn một frame 64 ms mới ổn định. Nó dùng instance
Silero **riêng** — cái thứ ba, bên cạnh gate đầu vào và noise guard của realtime
— để state LSTM của các đường kia không bị bẩn, và nó reset state đó ở đầu mỗi
phiên. Nó fail-open: model lỗi thì coi như có tiếng nói, nên thiết bị không bao
giờ cắt lời ai.

### Mic bỏ qua chính cue backchannel của mình

Cue lắng nghe của backchannel ("Ok", "Mm", "Oh") được phát mà **không** set cờ
`speaking` của TTS — cố ý, vì cờ đó sẽ kết thúc session STT đang chạy, đúng cái
session mà cue sinh ra để giữ. Nhưng `speaking` cũng là thứ duy nhất bình thường
giữ mic tắt khi thiết bị đang nói, nên cue lọt thẳng vào mic và VAD đầu vào mở một
session **mới** trên chính nó khoảng một giây sau. Quan sát trên thiết bị
19/08/2026: `'Ok'` quay lại thành `transcript='Okay.'` và `'Oh'` thành
`transcript='no'`, mỗi cái chạy thành một lượt thật mà không ai nói.

`Backchannel.self_audio_active` bịt lỗ này mà không đụng `speaking`. `_play()` cài
một deadline (độ dài clip + `HAL_BACKCHANNEL_ECHO_TAIL_S`) *trước khi* sample đầu
tiên phát ra, rồi neo lại phần đuôi theo thời điểm playback thực sự kết thúc. Trong
lúc deadline còn hiệu lực, vòng VAD bỏ các frame đó khỏi phép thử speech **và**
khỏi lookback pre-roll — giữ chúng trong lookback thì cue sẽ thành audio mở đầu của
session kế — rồi reset LSTM của Silero khi resume, đúng phần dọn dẹp mà warm-mic
drain vẫn làm. Chỉ chặn việc **mở** session; session đang stream không bị đụng, đó
mới là mục đích của tính năng.

Mỗi cue còn được buộc vào epoch của phiên STT đã yêu cầu nó. Nếu TTS bình thường
giữ output stream đủ lâu để phiên gốc kết thúc, cue đang chờ bị huỷ ngay trước lúc
phát; nó không thể lọt vào một phiên mic mới thành transcript bịa. Cơ chế này chỉ
huỷ lời nói tuỳ chọn của thiết bị — không đóng, xoá hay mute mic của người dùng, nên
người dùng vẫn có thể nói đè lên nó.

`robots/lamp/rootfs/opt/hal/.env` hạ `HAL_MAX_SESSION_DURATION_S` xuống `20`
(default trong code vẫn là `30`); trần đó chỉ chạm tới khi đồng hồ im lặng không
bao giờ hết hạn, mà người nói thật luôn ngừng lâu hơn `SILENCE_TIMEOUT` trong
vòng 20 giây. Cũng file đó trước kia ghi `WAKEWORD_FOLLOWUP_TIMEOUT_S=60` mà
thiếu prefix `HAL_`, nên nó không có tác dụng gì và thiết bị chạy default 20 s;
nay key đã là `HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S=60`.

Nếu kết nối provider **ban đầu** lỗi ngay khi HAL khởi động, orchestrator tạo
session mới bằng retry loop nền (thử lại một lần ngay, rồi backoff luỹ thừa từ 2s,
tối đa 60s). Nó tách biệt
với reconnect send/receive của provider, vì các loop đó chưa tồn tại trước khi
`connect()` thành công. Không cần restart HAL hay chờ audio mới; các lượt voice
vẫn fallback xuống agent chính cho tới khi kết nối hồi phục.

## Khử vọng âm (AEC)

`hal/drivers/voice/aec.py` đưa audio mic qua APM của WebRTC (AEC3), lấy audio
đang phát làm tín hiệu tham chiếu. Nó **độc lập với provider**: tham chiếu được
lấy tại `_WatchedStream.write` (`tts/service.py`) — điểm duy nhất mà mọi đường
phát ra loa đều đi qua: giọng tổng hợp, phần drain của `speak_queue`, và **native
audio** của realtime. Lấy ở đó thay vì tại lúc tổng hợp là có chủ ý: TTS render
một câu nhanh hơn thời gian thực rất nhiều, còn output stream ghi đúng tốc độ
phát — đúng nhịp mà mic nghe thấy.

**Mặc định bật** (`HAL_AEC_ENABLED=true`). Nếu thiếu binding bên dưới thì mọi
điểm vào AEC đều thành no-op, nên bật mặc định không thể làm hỏng thiết bị không
có nó. Nó cần binding `aec-audio-processing`, vốn **không** phải dependency gốc
của hal — PyPI không có wheel Linux nào, nên thiết bị phải build từ source. Nó
nằm sau extra `aec` (`uv sync --extra aec`), cố ý để ngoài `dependencies` và
ngoài `hardware`: bước build cần meson/ninja mà image lamp không cài, nên khai
hard dep sẽ làm hỏng cả build image lẫn `software-update hal` cho một tính năng
vốn tự thành no-op khi thiếu nó. Khi import thất bại, `configure()` log một lần
và mọi entry point trở thành no-op; đường voice hoạt động y như trước, nên mặc
định bật vẫn an toàn với thiết bị không có binding.

Bộ khử vọng chỉ làm sạch mic; nó không quyết định việc cắt lời. Trên đường lượt
không có gì lắng nghe mic đã khử để bắt người dùng nói đè lên câu trả lời — một
bộ phát hiện cục bộ đã được thử và gỡ bỏ sau phép đo ghi lại bên dưới. Người
dùng cắt lời bằng cách chạm (nút GPIO, touchpad TTP223), còn bên trong một phiên
live thì VAD của nhà cung cấp làm chủ việc cắt lời (xem *Chế độ live*).

| Env | Mặc định | Ý nghĩa |
|-----|----------|---------|
| `HAL_AEC_ENABLED` | `true` | Công tắc chính |
| `HAL_AEC_DELAY_MS` | `205` | Gợi ý độ trễ loa→mic. **Theo từng thiết bị** — phải đo, đừng chép lại |
| `HAL_AEC_NS` | `true` | Bật thêm khử nhiễu của APM. Trên phần cứng này nó gánh phần lớn việc khử |
| `HAL_AEC_TAIL_S` | `2.0` | Tiếp tục khử trong khoảng này sau lần ghi loa cuối, rồi bypass APM |
| `HAL_AEC_REF_MS` | `500` | Độ sâu FIFO của tham chiếu vọng âm |
| `HAL_AEC_DUMP_DIR` | — | Ghi `aec_mic/ref/out.wav` để phân tích ERLE offline |

### Cài binding

PyPI chỉ phát hành **wheel Windows** cho `aec-audio-processing`, nên mọi nền
tảng khác phải build từ sdist. Sdist đó đã vendor sẵn toàn bộ source
webrtc-audio-processing + abseil và một wrapper SWIG sinh sẵn, nên build khép
kín: không cần `libwebrtc-audio-processing` của hệ thống, cũng không cần SWIG hệ
thống. Các build requirement của nó (`swig`, `meson`, `ninja`, `cmake`) đều có
wheel trên PyPI, nên **không cần `apt install` gì cả** — điều này quan trọng, vì
người dùng cuối không thể chạy apt trên thiết bị đã xuất xưởng.

Vấn đề nằm ở chính bước build: đo trên lamp (A523, 8 core) mất **5m35s thực /
36m CPU**. Chạy một lần trên máy của dev thì được; chạy trên mọi thiết bị, mọi
lần build image và mọi lần `software-update hal` thì không. Nên dự án build một
wheel rồi đính vào một GitHub release:

```bash
scripts/release/build-aec-wheel.sh <device-ip>   # → dist/aec/*.whl
make upload-aec-wheel                            # → CDN, in ra URL + sha256
```

`build-aec-wheel.sh` biên dịch ngay trên thiết bị, trong `/tmp`, bằng một venv
dùng xong bỏ với meson/ninja lấy từ PyPI — `/opt/hal` và các gói hệ thống không
bị đụng tới — rồi copy wheel về, cài vào một venv sạch để chứng minh nó import
được, và xoá thư mục tạm.

**Build trên máy CŨ nhất, không phải máy mới nhất.** Wheel chỉ link
`libstdc++/libm/libgcc_s/libc` và cần **glibc ≥ 2.34**. glibc tương thích tiến,
nên wheel build trên lamp (Debian 12, glibc 2.36) chạy được trên Reachy Mini
(Debian 13, glibc 2.41) — chiều ngược lại thì không. Wheel mang tag
`cp312-cp312-linux_aarch64`: `uv` trên mọi body đều chạy CPython 3.12 nên tag đó
phủ hết đội máy, và `upload-aec-wheel.sh` từ chối publish thứ khác thay vì để
lệch ABI lộ ra trên máy khách hàng.

Asset nằm trên một tag riêng cho wheel (`wheels/aec-<version>`), không phải tag
phiên bản OS — wheel không đi theo nhịp release của OS, và tag riêng thì không bị
trỏ lại, nên URL đã pin không thể đổi nội dung sau lưng lockfile. Chọn GitHub
release thay vì bucket OTA là có chủ đích: repo này public nên một fork có thể tự
build và tự host wheel của họ, còn bucket thì chỉ nội bộ org.

`hal/pyproject.toml` pin URL đó trong `[tool.uv.sources]`, giới hạn ở
linux/aarch64/CPython 3.12. Đo trên `lamp-0c89`: cài wheel host sẵn mất **1.9
giây**, so với **5m35s** nếu compile. Nằm ngoài marker đó — máy Mac của dev, hay
3.13 sau này — sẽ rơi về sdist trên PyPI và compile, nên `uv sync --extra aec`
lúc nào cũng chạy; chỉ đường nhanh mới được pin.

Vòng VAD chính được bọc, và với `HAL_WARM_MIC=true` (nay là mặc định) mic vẫn mở
suốt lúc phát, nên việc khử chạy ngay trong lúc thiết bị đang nói. Cổng reverb
cố ý không khử để giữ nguyên timing.

**Đo trên lamp** (OrangePi sun60 / A523, mic USB + loa USB — hai miền clock độc
lập). Gợi ý độ trễ phải theo từng thiết bị vì hai clock USB chạy tự do: trên
`lamp-ee17` độ trễ thật là 204 ms (trung vị, một lượt 93 s) và 192 ms ở lượt
khác, trôi 154→215 ms ngay trong một lượt (~667 ppm). Sửa 150→205 đưa ERLE đạt
được từ 15.2 lên 17.9 dB. Trước đó sửa 80→150 trên cùng máy đưa từ 10.9 lên
18.6 dB.

Chi phí ~3.9 % một core A523 ở thời gian thực. Cùng bộ khử này trên MacBook đạt
~42 dB; khoảng cách là do phần cứng — hai clock USB tự do và đường analog rẻ.
Chỉ ~1.6 dB vọng âm ở đây là dự đoán được **tuyến tính** (coherence 0.31), nên
gần như toàn bộ việc khử là suppression — đó là lý do tắt `HAL_AEC_NS` mất ~10 dB
ERLE và làm residual tăng gấp ba.

### Hạn chế đã biết: tham chiếu bị đói

`EchoReference` là một FIFO được lấy tại lúc ALSA **nhận** audio, nhưng mic chỉ
nghe thấy audio đó sau trọn một output buffer, còn TTS thì ghi theo từng cụm
theo nhịp mạng. Khi phần ghi chạy trước xa hơn độ sâu FIFO, những byte cũ nhất —
đúng những byte mic sắp nghe — bị bỏ, và tham chiếu cạn khô cho phần còn lại của
cụm. Đo trên `lamp-ee17`: tham chiếu **underrun trên 30–86 % số khung xử lý**
trong một lượt trả lời, và ERLE mỗi cửa sổ dao động từ −25.1 dB tới 23.2 dB theo
đó. Ở những khung *có* tham chiếu, bộ khử đạt 15–23 dB — nên thiếu hụt là do đói
tham chiếu, không phải do APM.

Tăng độ sâu FIFO không sửa được mà còn tệ hơn (`HAL_AEC_REF_MS=1500` đo được
3.6 / 2.3 dB so với 23.2 / 19.1 dB ở 500) vì độ trễ dẫn trước trở nên thay đổi
và vượt cửa sổ căn chỉnh của AEC3. Cách sửa thật sự là ghi tham chiếu theo nhịp
**phát** thay vì nhịp ghi, cộng với một thread capture riêng để mic thôi rút
`arecord` theo từng cụm.

Nửa "theo nhịp phát" đã làm: `_WatchedStream.write` cắt mỗi buffer của caller
thành các lát `TTS_REF_SLICE_S` (40 ms) và chỉ ghi tham chiếu **sau** khi thiết
bị đã nhận lát đó, nên vòng lặp chạy xấp xỉ tốc độ loa phát. Kích thước lát là
đánh đổi về GIL chứ không phải về âm học — mỗi lát tốn một lần blocking write
xuống PortAudio cộng một lần ghi tham chiếu, đều trong Python; ở mức 10 ms thì
~1600 vòng mỗi câu trả lời nghe thành **giật tiếng** trên board mà thread chính
đã bị vision chiếm gần hết. Thread capture riêng vẫn chưa làm.

WAV cache (gồm lời xác nhận khi single-click) cũng ghi qua wrapper theo block
40 ms. Vòng ngoài 10 ms trước đây làm mất tác dụng gom block của wrapper và
vẫn chạy 100 lượt ghi loa/AEC mỗi giây khi vision đang tải. Nay phát cache dùng
25 lượt mỗi giây, cộng block cuối nếu còn dư, và kiểm tra hủy giữa các block.
Khoảng cách giữa hai lần kiểm tra stop có thể tăng tối đa 30 ms so với vòng cũ.
Đây không phải giới hạn thời gian loa ngừng tiếng: audio đã xếp trong buffer
thiết bị vẫn có thể còn phát sau khi stop.

TTS thông thường qua provider (gồm ElevenLabs PCM 24 kHz phát ở 44.1 kHz)
dùng nội suy tuyến tính liên tục qua các chunk PCM từ mạng trong từng yêu cầu
tổng hợp. Bộ resample giữ mẫu tại biên và clock mẫu thay vì bắt đầu lại nội suy
ở mỗi chunk. Khi EOF bình thường, mẫu cuối được giữ lại sẽ được xuất để bảo đảm
`ceil(N * output_rate / input_rate)` mẫu đầu ra với `N` mẫu đầu vào; khi hủy thì
không xuất phần đuôi này. Các yêu cầu tổng hợp phần đầu, phần đuôi và trong hàng
đợi có trạng thái resample riêng. Resample native realtime và resample toàn
file WAV cache không đổi.

Resample tham chiếu AEC cache hệ số FIR Kaiser mặc định của SciPy theo tỉ lệ
tần số lấy mẫu đã rút gọn và dtype (tối đa 32 mục), tránh thiết kế lại bộ lọc
ở mỗi lần ghi loa. `resample_poly` vẫn xử lý gain và padding như trước; dạng
sóng tham chiếu và nhịp ghi FIFO không đổi. Trước lần ghi loa đầu của mỗi lượt
phát, HAL chuẩn bị bộ lọc tham chiếu để lần import SciPy/thiết kế bộ lọc đầu
tiên không làm khựng sau 40 ms audio đầu. Bỏ qua chuẩn bị khi AEC chưa hoạt
động hoặc sample rate bằng nhau. Kiểm tra hủy giữa các lát, kể cả sau chuẩn bị;
chime xác nhận stop vẫn được phát khi cờ dừng lời nói đang bật.

Khi mở stream phát, hệ thống yêu cầu `max(0.120s, default_high_output_latency)`
và log độ trễ thực tế đã thương lượng. Mặc định 43.5 ms từng quan sát trên thiết
bị chỉ nhỉnh hơn một lát ghi 40 ms. Yêu cầu 120 ms tạo khoảng dự phòng lập lịch
bằng ba lát, đồng thời giữ mặc định lớn hơn của các đầu ra như Bluetooth; đây
không phải bảo đảm kích thước buffer hay cách sửa jitter mạng. Audio xếp hàng
nhiều hơn có thể kéo dài phần tiếng còn nghe sau khi hủy.

`_WatchedStream.write` gộp các cờ underflow của PortAudio qua mọi lát ghi.
Underflow giữa lúc phát được log tối đa một lần mỗi 5 giây với số lần tích lũy,
`writer_gap_ms` và `previous_aec_ms`; ranh giới lượt phát (có thể sau idle) chỉ
log debug, còn
các lần ghi keepalive bị loại khỏi thống kê. Underflow đầu ra báo thiếu audio
để phát; AEC reference underrun chỉ báo thiếu mẫu tham chiếu khử vọng và không
chứng minh loa bị underrun. Các thay đổi phát audio cục bộ này vẫn cần được
xác minh bằng nghe thử trên phần cứng.

`aec.uncancelled()` cho biết khung vừa đọc có đi qua mà **không** được khử thật
hay không — tham chiếu underrun, stream bị bypass, hoặc mic overrun. Cổng đường
lên của chế độ live dựa vào cờ này ở chế độ `cancelled` để không bao giờ gửi vọng
âm thô lên như thể là người dùng. Lưu ý điều nó **không**
nói: nó báo tham chiếu có *tới* hay không, chứ không báo việc khử có *hiệu quả*
hay không — một khung ERLE 0,9 dB vẫn được tính là đã khử.

### Vì sao không có cắt lời bằng giọng nói trên mic đã khử vọng

Một bộ phát hiện cục bộ ("barge-in": dừng TTS khi người dùng nói đè lên) đã
chạy từ 25/08 tới 13/09/2026 rồi bị gỡ bỏ. Các phép đo được giữ lại ở đây để
không ai phải thử lại từ đầu. Phần dư sót lại sau khi khử đủ to để trông như
người đang chen ngang, và nó **đúng là** tiếng nói, nên cả cổng mức lẫn bộ phân
loại speech đều không loại được. Đo trong phòng im, trần vọng âm so với lần chen
ngang thật:

| Âm lượng loa | Mixer | Trần vọng âm | Người chen ngang thật |
|---|---|---|---|
| 25 % (`lamp-ee17`) | −45 dB | 9804 | 8027 |
| 40 % (`lamp-0c89`) | −36 dB | 9969 | 6956–8027 |
| 65 % (`lamp-0c89`) | −21 dB | 13560 | 6956 |

Trần vọng âm nằm **trên** mức người thật ở mọi âm lượng, nên ngưỡng đặt dưới nó
thì đèn tự cắt lời mình, đặt trên nó thì bỏ sót giọng nói bình thường. Hạ âm
lượng loa cũng không phải cách chữa: cả 24 dB dải mixer chỉ kéo trần xuống chưa
tới 3 dB, vì đường ghép không do đường truyền qua không khí chi phối.

Phòng vệ cuối cùng được thử là phép so đường bao trên mic **thô**: căn cửa sổ
ứng viên với tham chiếu đang giữ, trừ nó cộng hệ số ghép đã học, rồi đọc độ
*lệch* của phần dư thay vì độ lớn — người chỉ có thể thêm năng lượng, nên phần
dư một chiều là có người khác trong phòng. Gán nhãn trên `lamp-0c89` ở 40 %:

| | Độ lệch phần dư |
|---|---|
| Vọng âm, phòng im (15 cửa sổ) | −2.8 … +2.1 dB |
| Vọng âm, mẻ trộn (~40 cửa sổ) | −50.0 … **+4.8** dB |
| Người chen ngang đã xác nhận | **+8.4** … +40.4 dB |

Trông có vẻ tách được, và 12 câu trả lời trong phòng im không bắn nhầm lần nào.
Nhưng nó không trụ được: 27/08/2026, 20 câu trả lời trong phòng im, đèn tự cắt
lời mình 4 lần; chấm 79 cửa sổ đã gán nhãn (69 vọng âm, 10 lần chen ngang đã xác
nhận) trên mọi đặc trưng sẵn có — tương quan đường bao, độ lệch phần dư, độ lệch
ghép, mức nén của APM, và magnitude-squared coherence kinh điển — cho AUC tốt
nhất 0.72, và mọi ngưỡng đạt 0 % tự cắt lời đều bỏ sót 90–100 % lần chen ngang
thật:

| đặc trưng | vọng âm | người |
|---|---|---|
| coherence | 0.03 … 0.60 | 0.04 … 0.57 |
| tương quan | 0.25 … 0.99 | 0.41 … 0.95 |
| độ lệch (dB) | −6.5 … 67.1 | −1.0 … 119.7 |

Hai lớp chồng lấn gần như hoàn toàn, nên không bộ phân loại nào trên tín hiệu
này làm tốt hơn được. Nguyên nhân nằm ở thượng nguồn: AEC3 chỉ đạt ~6 dB ERLE
trên phần cứng này vì loa và mic là hai thiết bị USB với clock chạy tự do, và
khi hai bên cùng nói, APM không làm nhỏ người nói gần — nó xoá luôn (một khung
đọc 7426 trên mic thô ra khỏi APM còn 5). So trên tín hiệu đã khử thay vì mic
thô cũng đã thử và còn tệ hơn: APM là bộ khuếch đại thay đổi theo thời gian, nó
ăn mất đường bao.

Chỉ thử lại khi chính đường vọng âm tốt lên — bộ khử neural (DTLN-aec chạy thời
gian thực trên Pi 3 B+) hoặc một sound card cho cả hai chiều — và gắn lần thử đó
với bài kiểm tra chấp nhận ở *Chế độ live*: phát lại
`barge-in-captures/full40-bargein-off` qua bộ khử và đòi residual đỉnh nằm dưới
sàn chen ngang thật (6956) với biên an toàn. Trước đó, chạm-để-cắt-lời và VAD
của nhà cung cấp là hai đường cắt lời duy nhất.

`process()` gom audio về khung cố định 10 ms của APM và trả về đúng số mẫu mà
caller yêu cầu (mồi một lần bằng tối đa 10 ms im lặng), nên khung 64 ms của hal
không đổi. ERLE được log định kỳ khi loa đang hoạt động — **0 dB nghĩa là bộ khử
không làm gì cả**.

> Image đã load sẵn `module-echo-cancel` của PulseAudio (`setup.sh`), nhưng
> không có gì đi tới nó: một udev rule đặt `PULSE_IGNORE=1` cho card loa để hal
> tự sở hữu, còn capture đi thẳng qua `arecord -D plughw:`. Module đó không có
> tham chiếu lẫn client; nó không phải thứ đang khử vọng âm ở đây.

## Biểu cảm cảm xúc (fire-and-forget)

Nếu thiết bị khai báo capability `expression`
(`ROBOT.md` → `expression: { routes: [emotion] }`), orchestrator còn đăng ký
thêm tool `express_emotion` (`orchestrator.py`, `EMOTION_TOOL`). Thiết bị không
có "mặt" (vd: chỉ mic + loa) sẽ không có tool này, nên model realtime không thể
set cảm xúc — gating chạy xuyên suốt: `server.py`
(`"expression" in _profile.capabilities`) →
`VoiceService(enable_expression=…)` →
`RealtimeOrchestrator(enable_expression=…)`.

Khác với `delegate_to_main`, `express_emotion` là **fire-and-forget** và là
ngoại lệ duy nhất của quy tắc "tool HOẶC nói" — model gọi nó *song song* với
việc nói. Khi `stream_output()` thấy lời gọi (`_handle_emotion_call`), nó:

1. gọi handler emotion của HAL **in-process** (`_fire_emotion` →
   `routes/emotion.py` `express_emotion`) trong một daemon thread — realtime agent
   chạy ngay trong process HAL nên không cần loopback HTTP / serialize. Nó chạy
   song song với audio đang stream, nên mặt đổi mà không chặn giọng;
2. trả lời lời gọi bằng `FunctionCallResultInput`, trong đó `trigger_response`
   phụ thuộc việc model đã nói trong lượt này hay chưa (`orchestrator.py`,
   `_handle_emotion_call`). Nếu **đã** nói (`trigger_response=False`), kết quả
   được ghi lại mà không sinh response thứ hai — với OpenAI và Qwen điều này bỏ
   qua `response.create` (`openai_realtime.py` / `qwen_realtime.py`); với Gemini
   thì ack **không được gửi đi**, vì `send_tool_response` ở đó làm lượt *tiếp
   tục* và model nói lại toàn bộ câu trả lời. Nếu **chưa** nói, tool call chính
   là toàn bộ phần model sinh ra cho tới lúc đó và Gemini dừng chờ, nên phải gửi
   ack (`trigger_response=True`) nếu không lượt sẽ treo tới khi watchdog nổ. Độ
   trễ cộng thêm vào giọng nói ≈ 0.

### Cô lập session khi tool call đang chờ (Gemini)

Gemini Live **từ chối `send_realtime_input` khi một tool call do nó phát ra chưa
được trả lời**, và cưỡng chế bằng cách đóng session với WebSocket **`1008`**
("The operation was aborted"). Đây là provider chủ động đóng theo policy, không
phải stream bị rớt — rớt ở tầng transport hiện ra là `1006` với reason rỗng và
được xử lý ở proxy, không phải ở đây.

Vì vậy `gemini_live.py` cách ly **toàn bộ phía client** của session đó, thay vì
chỉ chặn audio từ mic:

- nhận `tool_call` thì đăng ký mọi `call_id` vào `_pending_tool_calls` và làm
  session không thể gửi thêm dữ liệu;
- khi còn bất kỳ call nào chưa được giải quyết, **mọi input từ client đều bị
  chặn**: `AudioInput`, `activityStart` manual VAD, `activityEnd`, commit và các
  message client khác. Không buffer để phát lại, vì như vậy lời nói thu trong
  trạng thái provider không hợp lệ sẽ thành một lượt cũ ở thời điểm sau;
- với `FunctionCallResultInput` thông thường, call vẫn pending đến khi Gemini
  đã chấp nhận `send_tool_response`. Chỉ provider acknowledgement thành công đó
  mới xoá call và làm session hiện tại dùng lại được. Ack thất bại hoặc bị từ
  chối giữ session ở trạng thái cách ly và session sẽ bị bỏ;
- path `express_emotion` fire-and-forget phía trên cố ý không gửi acknowledgement
  cho Gemini khi model đã bắt đầu nói, vì gửi nó làm Gemini lặp lại câu trả lời.
  Session như vậy không thể hợp lệ trở lại: nó không được dùng lại, và lần
  `prepare_turn()` tiếp theo sẽ rebuild một session mới;
- không có expiry hay timeout nào mở lại một session đang cách ly. Session
  fresh/rebuild không thừa kế pending call.

Đặc biệt, `_async_commit` cũng chặn `activityEnd` khi session bị cách ly. Hoàn
tất activity bracket cũ không an toàn khi Gemini còn chờ tool result; session
thay thế sẽ bắt đầu activity kế tiếp một cách sạch sẽ.

Model được dặn (`resources/system_prompt*.md`, mục "Expression Exception") không
chờ, không thông báo, không đọc tên cảm xúc thành tiếng. Lưu ý điều này khác
path không-realtime: ở đó agent phát marker text `[HW:/emotion:…]` rồi lớp Go
parse và cắt bỏ — path realtime không bao giờ dùng marker text.

## Google Search grounding (chỉ Gemini)

Mặc định Gemini Live được cấp sẵn tool **Google Search** built-in
(`HAL_GEMINI_GOOGLE_SEARCH`, mặc định bật; wiring trong `gemini_live.py` như một
`types.Tool(google_search=…)` riêng, đặt cạnh các tool function-declaration). Nhờ
đó model realtime tự trả lời các câu **dữ liệu công khai theo thời gian thực** —
thời tiết, tin tức, thể thao, giá cả, "mấy giờ mặt trời lặn" — bằng cách grounding
ngay trong phiên và tự nói kết quả, thay vì gọi `delegate_to_main` và chịu nguyên
một vòng round-trip xuống main agent. System prompt của Gemini
(`system_prompt_gemini.md`) xếp các lookup công khai này vào mục *Direct Home Run*,
và chỉ chuyển dữ liệu live **thuộc tài khoản/riêng tư** (lịch của user, trạng thái
thiết bị smart-home của họ, tin nhắn của họ) cho `delegate_to_main`.

Đánh đổi:

- **Chỉ Gemini.** OpenAI Realtime không có tool built-in tương đương, nên prompt
  của nó (`system_prompt_openai.md`) vẫn delegate mọi lookup bên ngoài. Qwen Omni
  Realtime cũng vậy — không có search grounding, prompt của nó
  (`system_prompt_qwen.md`) delegate mọi câu dữ liệu thời gian thực.
- **Chi phí.** Grounding tính phí theo mỗi grounded request (cộng thêm token),
  nhưng chỉ phát sinh khi Gemini thực sự quyết định search. Prompt dặn nó *chỉ*
  ground cho dữ kiện công khai/mới thật sự, không ground cho kiến thức chung đã có
  sẵn. So với trước, phần lớn là **dời** chi phí (và latency) khỏi main agent.
- **Chỉ đọc.** Grounding chỉ trả lời câu hỏi, không thực hiện hành động. Nhạc,
  phần cứng, ghi memory, và skill vẫn delegate.

## Thị giác trong phiên — tool `look` (chỉ Gemini)

Khi người dùng hỏi về thứ thiết bị **nhìn thấy** ("cái này là gì?", "nhìn cái này
nè", "nhìn thứ tôi đang cầm", "tôi đang cầm gì?", "đọc cái nhãn này", "màu gì đây?"),
model realtime trả lời ngay trong phiên thay vì delegate. Lưu ý "nhìn cái này" đi vào
đường này, **không phải** đường bật/tắt camera riêng tư — `skills/camera/SKILL.md`
phân biệt động từ theo thứ đứng sau nó, vì "nhìn tôi này" nghĩa là "bật camera lên"
còn "nhìn cái này" là một câu hỏi về một vật. Chỉ áp dụng cho turn **thuần** hỏi về
thứ nhìn thấy: nếu cùng turn còn kèm hành động ("quay sang phải, giữ nguyên đó, rồi
nói xem thấy gì"), prompt bắt buộc gọi một `delegate_to_main` gộp cả hai vế — không
`look` — để lệnh chuyển động không bị âm thầm bỏ rơi. Orchestrator đăng ký tool `look` (`orchestrator.py`,
`LOOK_TOOL`) và xử lý trong `_handle_look_call`:

1. **Ngắm đầu vào đối tượng trước**, trên thiết bị có thể chuyển động — nếu
   không thì model sẽ trả lời đầy tự tin về bất cứ thứ gì cái đầu tình cờ đang
   hướng tới. Xem
   [Look-aim](../../robots/lamp/docs/vi/vision-tracking_vi.md#look-aim--ngắm-đầu-trước-khi-một-câu-hỏi-thị-giác-chụp-ảnh)
   để biết vòng lặp ngắm, cách nó chọn ai mới là người đang hỏi, và bearing đã
   ghi nhớ mà nó quay về khi không thấy ai.
2. Lấy frame camera **nét** **in-process** (`_capture_frame` gọi
   `capture_still` — không qua HTTP loopback; servo bị freeze (cả animation
   loop lẫn servo worker của tracker đều tôn trọng cờ này) và frame chỉ được
   chấp nhận khi timestamp chụp vượt quá thời gian chờ lắng tính từ lần ghi bus
   servo cuối, nên motion blur không lọt tới model. Thời gian lắng là 0.3s, co
   giãn theo độ lớn của lần chỉnh ngắm cuối với trần 0.5s — một lần ngắm hết hạn
   chót sẽ thoát ra ngay sau một cú quay lớn, và cần đèn vẫn còn rung quá mốc
   300ms cố định; không thêm độ trễ khi servo vốn đang đứng yên hoặc thiết bị
   không có servo), downscale về `HAL_GEMINI_VISION_MAX_WIDTH`
   (mặc định 768px) để giới hạn token ảnh.
3. Đẩy vào làm **video input** realtime (`ImageInput` → `send_realtime_input(video=…)`),
   rồi **replay turn**: Live API xếp frame gửi giữa-turn vào turn KẾ TIẾP
   (device-proven: flow ack-tool → tiếp-turn cũ khiến mọi câu look trả lời bằng
   ảnh của lần look *trước* — lệch 1 ảnh, delay ack bao nhiêu cũng không cứu),
   nên thay vì ack tool call, orchestrator yield `LookReplaySignal` và
   `run_realtime_turn` gửi lại audio của turn + commit lần nữa trên CÙNG
   session. Frame đang xếp hàng vào đúng turn replay.
4. Turn replay kích hoạt `look` lần nữa, rơi vào reuse guard
   (`VISION_MIN_INTERVAL_S`) và được ack `trigger_response=True` — model trả
   lời bằng frame lúc này đã thật sự nằm trong context.

Plumbing hỗ trợ replay: `receive()` nuốt đúng MỘT `turn_complete` cũ (của turn
bị hủy, về sau replay commit và nếu không sẽ kết thúc rỗng turn replay —
`skip_next_turn_done()`); recycle session idle/turn-cap đang chờ sẽ bị hoãn khi
replay pending (rebuild lúc đó làm mồ côi ảnh vừa gửi); và mọi lần rebuild
session đều reset look reuse guard (ảnh sống trong session — session mới không
có ảnh nào). Chi phí: audio câu hỏi bị tính 2 lần ở turn look; ảnh 1 lần.

Cái này thay cho đường chậm (delegate → main → tìm skill → `/camera/snapshot` →
LLM vision, vài giây) bằng một round-trip ngay trong phiên.

Điều kiện kích hoạt (cần cả ba, nếu không câu hỏi thị giác sẽ rơi về delegate):

- **Capability:** có camera (`app_state.camera_capture` được set). Đây chính là
  capability `vision` ở runtime — `server.py` chỉ tạo `camera_capture` khi
  ROBOT.md khai báo `vision`. Orchestrator đọc đúng một tín hiệu này
  (`_camera_present()`), nên đúng cho mọi đường khởi tạo.
- **Flag:** `HAL_GEMINI_VISION` / `realtime.gemini.vision` (mặc định **bật**).
- **Provider:** chỉ Gemini (luồng inject ảnh → tiếp tục turn đã làm + test cho
  Gemini Live; OpenAI và Qwen vẫn delegate — Qwen Omni qua đường realtime này
  chỉ có text+audio, không có vision trong phiên). System prompt Gemini
  (`system_prompt_gemini.md`) mô tả khi nào gọi `look`.

Chi phí: một frame mỗi lần gọi (kích bằng tool, **không** stream video), nên token
thêm vào là không đáng kể so với audio của turn. Frame 768px ≈ vài trăm token ảnh.
Để chặn model gọi `look` quá nhiều làm tốn token ảnh, `_handle_look_call` chỉ gửi
**tối đa một ảnh mỗi turn** và **không gửi ảnh mới trong vòng
`HAL_GEMINI_VISION_MIN_INTERVAL_S` (mặc định 10s)** kể từ lần gửi trước — các lần
`look` lặp lại sẽ xài lại ảnh đã có trong context.

**Bàn giao frame khi delegate / timeout.** Khi một turn `look` rốt cuộc delegate
hoặc rớt xuống main agent (quan trọng nhất là khi Gemini timeout *giữa* lúc look),
frame mà `look` đã chụp được bàn giao cho main agent để nó trả lời từ đúng ảnh
đó thay vì chụp lại (nhanh hơn, và trả lời đúng khoảnh khắc user chỉ vào).
`_handle_look_call` lưu frame vào `_SNAPSHOT_DIR` và ghi vào
`app_state.realtime_look_frame_path`; `turn_dispatch._take_vision_handoff()`
tiêu thụ nó **một lần mỗi turn** (turn đã handled dùng rồi thì clear luôn để
delegate sau không nhặt phải ảnh cũ) và, khi còn tươi
(`HAL_GEMINI_VISION_HANDOFF_MAX_AGE_S`, mặc định 45s), chèn dòng hint
`[vision-image] <path>` vào message VÀ gửi frame dạng base64 trong field
`image` của sensing POST.
os-server xử lý ảnh theo **gate describe-first** trong `system/vision` (xem
`server/sensing/delivery/http/handler.go`): khi main model đang active KHÔNG
khai image input trong catalog model (trường hợp Auto-AI — attachment thô sẽ
404 tại smart-agent-router: "No endpoints found that support image input"),
frame được `default_image_model` của catalog (qwen — cùng model mà `imageModel`
của openclaw dùng cho ảnh Telegram) tả thành chữ và agent nhận dòng
`[image description] …` — đồng thời hint `[vision-image]` được viết lại để
**bỏ path file**, và **file snapshot cũng bị xoá luôn** (best-effort). Cả
path lẫn file đều không được sống chung với description: snapshot nằm trong
media allow-list của agent nên bất kỳ path nào agent vớ được — hint, hint cũ
trong session history, `ls` thư mục — đều có thể bị `read` thành image block
nằm lì trong session history, làm 404 mọi turn sau mà router rơi vào model
text-only (kể cả turn thuần chữ). Describe được thử 2 lần (20s + 15s, tổng
35s — request treo được retry trên kết nối mới); fail cả hai thì ảnh bị
**bỏ luôn**, file snapshot vẫn bị xoá, và hint được viết lại để agent nói
với user là lần này không nhìn được — tuyệt đối không gửi raw attachment,
vì khi router rơi vào model text-only thì attachment đó đầu độc cả session,
đắt hơn nhiều so với hỏng một turn. Còn khi catalog nói model nhận ảnh,
attachment thô được forward thẳng và hint giữ nguyên path. Gate đọc lại catalog mỗi 30 phút, nên BE flip catalog là
fleet tự chuyển. Gate này cũng cover luôn ảnh upload từ web monitor chat — cả
hai nguồn ảnh hội tụ về một handler. Skill `camera` dặn agent trả lời từ mô
tả/attachment và bỏ qua `/camera/snapshot`. Nếu timeout xảy ra *trước khi* kịp
chụp thì không có gì để bàn giao, agent chụp như bình thường.

## Các provider

Ba backend thay thế cho nhau, chọn bằng `HAL_REALTIME_PROVIDER`
(`none` | `gemini` | `openai` | `qwen`):

| Provider | Class | Mô hình threading | Model mặc định | Sample rate |
|----------|-------|-------------------|----------------|-------------|
| Gemini Live | `voice_agent/gemini_live.py` `GeminiLiveAgent` | event loop asyncio riêng trên thread `gemini-io`; thread send/recv submit coroutine qua `run_coroutine_threadsafe` | `gemini-2.5-flash-native-audio-preview-12-2025` | 16000 Hz |
| OpenAI Realtime | `voice_agent/openai_realtime.py` `OpenAIRealtimeAgent` | thuần đồng bộ; 1 `RealtimeConnection` dùng chung bởi thread send/recv, serialize bằng reentrant lock | `gpt-realtime-2` | 24000 Hz |
| Qwen Omni Realtime | `voice_agent/qwen_realtime.py` `QwenRealtimeAgent` | thuần đồng bộ; client `websockets.sync.client` thô | `qwen3.5-omni-plus-realtime` | 16000 Hz |

Gemini Live dùng `google-genai` và private asyncio loop của nó do thread
`gemini-io` sở hữu. Teardown đóng/hủy provider receive task trước, rồi mới join
worker; handshake thất bại rollback loop/thread ngay. Nhờ vậy một receive bị
kẹt không sống sót qua session rebuild. Với họ native-audio, HAL gửi websocket
ping mỗi 20 giây nhưng không đặt ping timeout: traffic đi ra giữ đường proxy
sống mà pong bị thiếu không bị hiểu là lỗi client. HAL cũng
recycle Gemini đồng bộ trước khi stream audio nếu lượt trước đã kết thúc quá
`HAL_GEMINI_PRE_TURN_RECYCLE_S` giây, để câu nói sau khoảng nghỉ không rơi vào
socket đã chết vì idle ở proxy.

**Park khi idle.** Session Gemini không ai nói chuyện cùng sẽ bị server đóng bằng
WS `1008` "The operation was aborted" (thời gian sống khi idle đo được: 86-198
giây). Cú đóng đó không làm hỏng turn nào — pre-turn recycle ở trên đã thay
session trước khi turn sau khoảng nghỉ stream audio — nhưng backend ghi nó là lỗi
và bắn cảnh báo, nên thiết bị phải đóng trước. Sau `HAL_GEMINI_IDLE_PARK_S` giây
không có hoạt động turn nào, thread watchdog `rt-idle-park` đóng transport và
đánh dấu session là *parked*. Orchestrator đang parked vẫn báo `available`:
`prepare_turn()` kế tiếp sẽ nối lại session mới một cách đồng bộ
(`idle-park-resume`) trước khi có audio nào được stream — đúng bằng việc mà
pre-turn recycle vốn đã làm cho turn đó — và `voice_service` giữ đệm capture qua
~1 giây handshake. Không park khi đang có turn chạy dở; nếu resume không nối
được thì báo unavailable (turn rơi về main agent) nhưng vẫn giữ trạng thái parked
để turn sau thử lại.

Mọi provider coi teardown là trạng thái kết thúc: sau khi `disconnect()` đặt
stop signal, worker send/receive không reconnect và cũng không ghi log lỗi
transport trong lúc socket đã đóng đang unwind.

**Qwen Omni Realtime** (Alibaba DashScope / Model Studio) nói **schema event BETA
của OpenAI Realtime** (`session.update`, `input_audio_buffer.append/commit`,
`response.create`, `response.audio.delta`, `response.audio_transcript.delta`,
`response.done`) qua đường WS của DashScope
`wss://<workspace-host>/api-ws/v1/realtime?model=...` với header
`Authorization: Bearer <key>`. Không tái dùng được OpenAI python SDK (SDK nói
schema GA), nên `qwen_realtime.py` là client `websockets.sync.client` thô. Audio
input 16 kHz mono pcm16 base64, output 24 kHz mono pcm16. Luồng turn thủ công
(HAL local VAD): append → commit → `response.create`; `response.create` **bắt
buộc** kèm `response.modalities ["text","audio"]` tường minh, nếu không server
trả lời text-only (verify live 2026-07-06). Model mặc định
`qwen3.5-omni-plus-realtime`: bản legacy `qwen-omni-turbo-realtime` KHÔNG bao
giờ gọi function call và lờ `[TURN CONTEXT]` (device-test 2026-07-06) → hỏng
toàn bộ luồng delegate. Voice: Ethan (mặc định) và Serena trên 3.5-plus;
Cherry/Chelsie chỉ dùng được với turbo (ghép sai → `InvalidParameter` ngay
response đầu); **không** có knob reasoning/thinking (web ẩn selector Reasoning).
Web search built-in (model 3.5) bật qua session `enable_search: true` (knob
`realtime.qwen.search` / `HAL_QWEN_SEARCH`, mặc định bật) — bản qwen của Google
Search grounding bên Gemini. Ràng buộc DashScope: search ("agent mode") KHÔNG
cho đăng ký function tools cùng session, nên khi search bật, delegate chạy qua
giao thức text-marker: agent nối suffix `[TOOL PROTOCOL]` vào instructions,
model trả lời đúng `[DELEGATE] <message>`, recv loop nuốt transcript đó và tổng
hợp FunctionCallOutput `delegate_to_main` y hệt tool call thật (orchestrator
không phân biệt được; `express_emotion` không dùng được ở mode này). Khi search
tắt, function tool (`delegate_to_main`, `express_emotion`) được truyền trong
`session.update` (format beta phẳng) và
`response.function_call_arguments.done` được xử lý. Mỗi turn, dòng token/cost
ghi vào file log riêng `qwen_usage.log` (logger `hal.realtime.usage.qwen`, sinh
đôi với `gemini_usage.log`); bảng giá `_QWEN_RATES` trong `qwen_realtime.py`
($0.27/1M input, $1.07/1M output — Model Studio quốc tế công bố một mức giá
blended duy nhất, chưa công bố tách theo modality; bảng vẫn giữ key theo
modality để drop số tách console-verified vào sau). Audio ≈ 25 token/giây cả
hai chiều (verify: 5.1s audio out = 128 token); usage payload gồm
`input_tokens`/`output_tokens` + `input_tokens_details`/`output_tokens_details`
`{text_tokens, audio_tokens}` + `cached_tokens` top-level.

Cả ba kế thừa `voice_agent/base.py` `VoiceAgentBase`, định nghĩa contract dựa
trên queue:

- **2 thread mỗi agent**: `_send_loop` rút `_send_queue` → API; `_recv_loop` đọc
  API → `_recv_queue`. Cả hai tự reconnect khi lỗi.
- **Fail-fast khi backend lỗi** (cả 2 driver): khi `_recv_loop` gặp lỗi thật
  (Gemini Live: proxy `go_away`, hết quota / resource-exhausted, WS close bất
  thường — tức **không phải** idle close `1000` lành tính; OpenAI: event `error`
  của Realtime API hoặc socket rớt), nó đẩy `TurnDoneEvent` ngay lập tức
  (`_fail_fast_turn`) để `receive()` thoát liền và lượt fallback sang main agent
  **mà không** phải chờ hết `HAL_REALTIME_RECV_QUEUE_TIMEOUT_S`. Idle close lành
  tính vẫn reconnect êm (Gemini code `1000`; OpenAI kết thúc vòng lặp event êm,
  không phải lỗi). Chỉ kích hoạt khi đang có lượt chờ output (`_turn_done` clear);
  reconnect vẫn chạy nền để hồi phục session cho lượt sau.
- **Non-blocking**: `append_audio()`, `commit_audio()`, `send()` (đẩy vào queue,
  gate trên `available`).
- **Blocking**: `connect()`, `disconnect()`, `receive()` (generator yield
  `OutputBase` đến khi gặp `TurnDoneEvent`, hoặc khi không có event nào trong
  `HAL_REALTIME_RECV_QUEUE_TIMEOUT_S` — mặc định 8 s — để kết thúc lượt im lặng
  và fallback sang main agent mà không bị dead-air dài).
  Riêng cửa sổ đó không phân biệt được model *quyết định không trả lời* với model
  *đang làm việc*: một lượt có Google Search grounding không phát ra output nào
  cho tới khi search về, và cắt ở đó là vứt đi câu trả lời model sắp nói, đẩy
  lượt xuống main agent chậm hơn nhiều, mà vẫn phải trả tiền cho cái search bị bỏ
  (chunk của nó rơi vào context session và bị tính lại ở mọi lượt sau). Nhưng
  trên đường truyền thì hai ca này khác nhau: lượt đang làm việc vẫn liên tục gửi
  message không bao giờ vào queue (thought part, grounding metadata, frame chỉ có
  usage), còn lượt bị bỏ thì im hoàn toàn. Provider gọi `note_server_activity()`
  ở mọi message nhận được, và `receive()` giữ lượt sống quá cửa sổ gap chừng nào
  còn message về, tối đa `HAL_REALTIME_TURN_MAX_SILENCE_S` (mặc định 20 s) cho cả
  lượt. Lượt không có message nào về vẫn kết thúc ngay ở cửa sổ gap đầu tiên.
- `available` ⇔ websocket/session đã connect (`_connected`).

### An toàn connection của OpenAI

Agent OpenAI dùng chung 1 `RealtimeConnection` giữa thread send và recv. Mọi
thao tác ghi vào connection, việc swap connection khi reconnect, và teardown đều
chạy dưới reentrant lock (`_conn_lock`); vòng lặp recv blocking dài chạy **ngoài**
lock trên một snapshot của connection để send audio không bị starve giữa lượt.
Reconnect là idempotent (re-check `_connected` trong lock) và `_drop_connection()`
chỉ null connection nếu nó vẫn là connection hiện tại — nên 2 thread không thể
tear down / dựng lại connection của nhau.

## Pricing & log usage

Mỗi turn ghi một dòng token/cost vào log riêng theo provider dưới
`/var/log/hal/` (rotating, 5 MB × 3): `gemini_usage.log` (logger
`hal.realtime.usage`) và `qwen_usage.log` (logger `hal.realtime.usage.qwen`).
OpenAI chỉ log dòng usage thường vào `server.log` (`[realtime] OpenAI usage`),
không ước tính cost. Dòng log mang đủ số token theo từng modality **và** cost
USD ước tính, nên rate có sai thì sau này vẫn tính lại được từ số token đã ghi.

Bảng rate nằm trong code, key `(direction, modality)` tính USD trên 1M token —
`_GEMINI_RATES` trong `voice_agent/gemini_live.py`, `_QWEN_RATES` trong
`voice_agent/qwen_realtime.py`. Model lạ rơi về bảng đắt nhất (cost là trần,
không bao giờ báo thiếu).

| Model | text in | audio in | text out | audio out | audio↔token | Nguồn |
|---|---|---|---|---|---|---|
| `gemini-2.5-flash-native-audio` | $0.50 | $3.00 | $2.00 | $12.00 | 25 tok/s | ai.google.dev pricing (verify 2026-06-29) |
| `gemini-3.1-flash-live` | $0.75 | $3.00 | $4.50 | $12.00 | 25 tok/s | ai.google.dev pricing (verify 2026-06-29) |
| `qwen-omni-turbo-realtime` | $0.27 | $4.44 | $8.89* | $8.89* | 25 tok/s in+out | bill CSV consume-detail (verify 2026-07-06); *output turn có audio bill gộp text+audio (`multi_output_token`); response text-only bill $1.07 (`purein_text_output`) |
| `qwen3.5-omni-flash-realtime` | $0.27 | $4.44 | $8.89* | $8.89* | ~7 tok/s in, ~12.5 tok/s out | bill CSV (verify 2026-07-06): flash bill CÙNG line item rẻ như turbo, kể cả phiên bật search — text-in (phần nặng nhất) rẻ hơn Gemini 3.1 ~2.8x. Search +$0.01/request |
| `qwen3.5-omni-plus-realtime` | $2.10 | $16.50 | $62.00* | $62.00* | ~7 tok/s in, ~12.5 tok/s out | bill CSV consume-detail (verify 2026-07-06); *một line item `omni_audio_output_token` bao cả text+audio của response. Web search tính thêm $0.01/lần search |

Cơ cấu chi phí giống nhau ở mọi provider: `in_text` chiếm áp đảo (system
prompt ~7-10k token + context session tích lũy bị re-bill mỗi turn, phình dần
tới khi session recycle — xem `HAL_REALTIME_SESSION_IDLE_RESET_S` /
`HAL_REALTIME_SESSION_MAX_TURNS`); token audio chỉ là phần lẻ. Gemini tính
thêm phí Google Search theo từng request grounded, ngoài token.

## Orchestrator

`orchestrator.py` `RealtimeOrchestrator` bọc một session agent và là bề mặt duy
nhất mà `voice_service` giao tiếp:

| Method | Mục đích |
|--------|----------|
| `start()` / `stop()` | Dựng agent từ config, connect, summarize memory khi tắt |
| `append_audio(frame)` | Đẩy 1 frame mic (non-blocking) |
| `commit_audio()` | Báo hết câu nói (non-blocking) |
| `stream_output()` | Yield `AudioOutput` / `TextOutput` / `FunctionCallOutput`, hoặc `DelegateSignal` (rồi dừng) |
| `send_text(text)` | Bơm context (turn context, TTS history) dạng user message không tạo response. Gemini Live bỏ qua bước này để tránh va chạm giữa SDK `clientContent` và lượt audio; OpenAI vẫn nhận. |
| `send_function_result(call_id, output)` | Trả kết quả tool về model |
| `save_turn(user, agent)` | Lưu một lượt vào realtime memory |
| `available` / `sample_rate` | Trạng thái sẵn sàng + sample rate của provider |
| `rebuilding` / `wait_until_available()` | Quan sát và chờ ngắn session thay thế vốn đang kết nối, không tự khởi động thêm rebuild |

## Context manager

System prompt, định danh thiết bị, device memory, và skills catalog được lắp ráp
theo agent gateway (`HAL_AGENT_GATEWAY`):

| Gateway | Class | Workspace |
|---------|-------|-----------|
| `openclaw` | `context_manager/openclaw.py` `OpenClawContextManager` | `HAL_OPENCLAW_WORKSPACE_DIR` (`/root/.openclaw/workspace`) |
| `hermes` | `context_manager/hermes.py` `HermesContextManager` | `HAL_HERMES_WORKSPACE_DIR` (`/root/.hermes`) |
| `picoclaw` | `OpenClawContextManager` (layout giống hệt) | `HAL_PICOCLAW_WORKSPACE_DIR` (`/root/.picoclaw/workspace`) |
| `codex` | `OpenClawContextManager` (layout giống hệt) | `HAL_CODEX_WORKSPACE_DIR` (`/root/.codex/workspace`) |
| `claudecode` | `context_manager/claudecode.py` `ClaudeCodeContextManager` — layout OpenClaw trừ skills, đọc từ `.claude/skills/` (dir native của claude CLI) | `HAL_CLAUDECODE_WORKSPACE_DIR` (`/root/.claudecode/workspace`) |
| `opencode` | `OpenClawContextManager` (layout giống hệt; như codex, skills nằm ở dir ngoài workspace `~/.config/opencode/skills` nên catalog skills theo workspace rỗng — identity + memory vẫn nạp đúng) | `HAL_OPENCODE_WORKSPACE_DIR` (`/root/.opencode/workspace`) |

`ContextManagerBase` (`context_manager/base.py`) lo phần lắp ráp prompt
(`build_instructions`), lưu lượt (`add_turn`), nạp/trim memory, và summarize;
subclass cài `load_device_context`, `load_device_memory`, `load_skills_catalog`,
`summarize_device_memory`. Prompt nền nằm ở `resources/` (`system_prompt.md` +
bản theo provider `system_prompt_openai.md` / `system_prompt_gemini.md` /
`system_prompt_qwen.md`, đăng ký trong `PROVIDER_PROMPT_PATHS` của
context_manager).

### Memory & summarization

Các lượt realtime được append vào file JSONL (`HAL_REALTIME_MEMORY_PATH`, mặc định
`<workspace>/realtime/memory.jsonl`), trim về `HAL_REALTIME_MAX_MEMORY_ENTRIES`
(giữ lại `HAL_REALTIME_MEMORY_TRIM_KEEP`). `RealtimeSummarizer` (`summarizer.py`)
nén device + realtime memory qua **Anthropic Messages API**
(`HAL_REALTIME_SUMMARIZER_MODEL`, mặc định `claude-haiku-4-5-20251001`).

Lời gọi này được **thử lại** (`HAL_REALTIME_SUMMARIZER_RETRIES`, mặc định 2,
giãn cách `HAL_REALTIME_SUMMARIZER_RETRY_BACKOFF_S`) vì lỗi đến từ gateway chứ
không từ input: đo trên lamp-0c89 ngày 03/09/2026, cùng một payload trả 404 một
lần rồi thành công 4 trong 5 lần kế tiếp (một lần timeout), trong khi payload
LỚN HƠN chứa trọn nó lại chạy ngay lần đầu. Không thử lại thì một cú rớt là mất
trọn bản tóm tắt cho tới lần rebuild session sau.
Điều này gồm cả lượt delegate hoặc fallback sang main agent: HAL lưu request của
user trước khi dispatch, rồi lưu từng fragment TTS opt-in của main agent sau khi
nói xong. `[TTS HISTORY]` vẫn cập nhật session live hiện tại ngay lập tức, nhưng
không được coi là memory bền: session mới sau idle hoặc tool-call sẽ nạp lại từ
JSONL/summary.
Với các runtime dùng layout OpenClaw (OpenClaw, PicoClaw, Codex, Claude Code,
OpenCode), context manager còn nạp `MEMORY.md` ở root của workspace, ngoài
device summary được sinh ra và các file `memory/*.md` mới. Hermes dùng
`memories/MEMORY.md` theo layout native của nó.
Summarize chạy lúc `start()` (bù phần chưa tóm tắt) và `stop()` (flush). Phần
catch-up ở `start()` chạy trong **thread nền** (sau `connect()`), nên lời gọi
Anthropic không chặn session trở thành `available` — nếu chặn thì một lượt nói
sớm ("hello") ngay sau khi restart sẽ rớt xuống main agent.

## Chế độ live (song công hoàn toàn)

**Nó thay đổi gì.** VAD cục bộ thôi không còn làm nhiệm vụ chốt lượt mà trở
thành **chuông cửa**: nó quyết định khi nào MỞ một phiên, và khi phiên đã mở thì
nó hoàn toàn không chạy nữa. Mic stream liên tục và **nhà cung cấp** sở hữu việc
chuyển lượt, kết thúc lượt và ngắt lời. Bật bằng `HAL_LIVE_MODE=true`.

Đo trên `intern-v2-6286` (07/09/2026, Gemini 3.1 Flash Live): **97 ms** từ lúc
gửi audio cuối tới lúc nhận audio đầu tiên, so với 3-6 s commit→câu nói đầu tiên
trên đường lượt.

**Loại trừ nhau theo thiết kế.** Đường lượt và phiên live cần chế độ nhận biết
lượt *ngược nhau*, và thiết lập đó được nướng vào phiên nhà cung cấp ngay lúc
kết nối. Hỗ trợ cả hai cùng lúc sẽ cần một override runtime cộng với việc dựng
lại phiên mỗi lần vào và ra; biến chế độ live thành lựa chọn cho toàn tiến trình
loại bỏ hẳn bộ máy đó, đổi lại phải khởi động lại để chuyển. Vì vậy
`HAL_LIVE_MODE=true` **ép** `HAL_REALTIME_TURN_DETECTION` từ `off` sang
`server_vad` (`hal/config.py`) — giá trị đó được đọc lúc import bởi
`GeminiConfig.vad_enabled`, nên phải chốt trước khi các model đó được định nghĩa.
Một giá trị khác `off` do người dùng đặt thì được giữ nguyên. Sai chỗ này sẽ tạo
ra thiết bị stream audio mãi mãi mà không bao giờ trả lời.

### Cổng vào: hai cửa, ba kết cục

`_vad_loop` xác nhận tiếng nói như thường lệ, rồi `_live_decision()` trả về một
trong:

| kết cục | khi nào | chi phí |
|---|---|---|
| `live` | trigger là tiếng nói và realtime khả dụng | một phiên live |
| `turn` | realtime tắt hoặc không khả dụng | đường lượt bình thường, thiết bị vẫn trả lời qua STT + main agent |
| `skip` | đang phát nhạc, hoặc trigger **không phải tiếng nói** | không tốn gì cả |

Cửa thứ hai không phải tùy chọn trên thiết bị có VAD vào rộng. Một phiên live
tính tiền audio lên suốt thời gian nó mở, nên nó không được kích hoạt vì một
tiếng "cạch" — và không thể tin cửa vào tự quyết việc đó: `webrtcvad` chấp nhận
7/7 mẫu không phải tiếng nói trong khi Silero từ chối cả 7, và
`HAL_SILERO_ENABLED` là `false` trên một số thiết bị. Do đó
`_rt_noise_is_speech()` mang theo instance Silero **của riêng nó**. Quan sát trên
`intern-v2-6286`: một transient codec bị clip đã mở một phiên mỗi ~25 s suốt cả
buổi tối. `skip` (chứ không phải `turn`) cũng là lý do một trigger bị từ chối
không tốn cả phiên STT.

### Cổng đường lên

Mọi khung mic đều tới model. Quyết định duy nhất mỗi khung là bộ khử vọng có bảo
đảm được cho nó không, và khung nào nó không bảo đảm được thì bị **thay bằng im
lặng cùng độ dài, không bao giờ bị bỏ** — đường lên là một đồng hồ, và một mối
nối chính là thứ mà VAD phía server đọc thành điểm bắt đầu nói. Việc thay thế
diễn ra *trước* khi resample, nên số khung ra == số khung vào theo cấu trúc.

`HAL_LIVE_UPLINK_DURING_PLAYBACK`:

- **`mute` (mặc định)** — thay im lặng cho toàn bộ cửa sổ phát. Dùng được ngay.
  Đánh đổi hoàn toàn khả năng cắt lời: người dùng không thể ngắt cho tới khi
  thiết bị nói xong.
- **`cancelled`** — gửi khung đã khử vọng; chỉ thay thế những khung mà
  `aec.uncancelled()` đánh dấu. Song công thật sự.

`cancelled` là đích đến và **không** phải mặc định, vì bộ khử vọng chưa xứng
đáng. Đo 04/09/2026 (`barge-in-captures/`): ERLE trung bình 14-19 dB nhưng **ERLE
đỉnh ~5 dB** — đỉnh mic 29264 rời APM còn 25269, so với sàn ngắt lời thật là
6956. VAD của nhà cung cấp nhìn thấy đỉnh và không có phòng vệ vọng âm nào
của riêng nó (không thấy được `uncancelled()`, không áp sàn thời lượng), nên nó
đọc chính điểm bắt đầu tiếng của thiết bị thành người dùng ngắt lời. Chuyển sang `cancelled` khi phát lại `full40-bargein-off` đưa residual đỉnh
xuống dưới sàn đó với biên an toàn.

Hai tín hiệu độc lập "loa có đang phát không" nuôi cổng này, vì không cái nào tự
đủ: `tts.speaking` là nguồn thẩm quyền và hoạt động **kể cả khi không có bộ khử
vọng nào** (khi không có, `aec.reference_idle_for()` trả `inf` và cổng sẽ không
bao giờ kích hoạt), còn phần đuôi reference bổ sung độ suy giảm âm học sau khi cờ
tắt. `HAL_LIVE_PLAYBACK_TAIL_S` là đuôi *âm học*, cố ý không phải `AEC_TAIL_S`
(2.0 s): nếu khóa theo cái dài hơn, `mute` sẽ nuốt hai giây đầu của mọi câu trả
lời người dùng nói.

**Nhạc được loại trừ bằng cách hỏi `music_service`, không bao giờ bằng cách hỏi
cổng này.** `aplay`/`paplay` ghi thẳng ra ALSA và không bao giờ chạm điểm trích
vọng, nên trong lúc phát nhạc `reference_idle_for()` đọc ra một căn phòng hoàn
toàn yên tĩnh.

### Bơm đầu ra

`_live_out_pump` lặp `orchestrator.stream_output()` thay vì đọc thẳng hàng đợi
của agent. Việc đó tái dùng toàn bộ bề mặt tool đang có — `look` + replay,
`express_emotion`, `reject_turn`, `delegate_to_main` — thay vì cài lại, và nó đọc
lại `self._agent` ở mỗi vòng ngoài, nên việc dựng lại phiên không thể để bơm đọc
một hàng đợi chết. `stream_output()` trả về một lần cho mỗi câu trả lời của model
(`turn_complete`) và cũng trả về khi im lặng lâu, lúc `receive()` hết giờ mà chưa
yield gì; cả hai đều chỉ có nghĩa là "quay lại vòng nữa".

`InterruptedOutput` (mới) được `gemini_live` phát ra khi `content.interrupted` và
là **cách cắt lời bằng giọng nói duy nhất** — không có gì cục bộ quyết định
nó. Nó dừng phát ngay lập tức. Đường lượt không bao giờ thấy nó: manual VAD
không cho server cơ hội phát ra.

**Đầu ra được flush một lần lúc bắt đầu phiên.** Không có nó, phiên sẽ mở trên
một đầu ra cũ của lượt trước — quan sát trên thiết bị 07/09/2026: một
`reject_turn` do lượt nhiễu trước đó xếp hàng đã bị đọc ngay trong cùng giây với
session START, và phiên ngồi im suốt toàn bộ thời gian chờ. `run_realtime_turn`
flush trước mỗi commit vì đúng lý do này; phiên live không commit gì, nên nó
flush lúc vào. Chỉ một lần — flush mỗi câu trả lời sẽ vứt mất đầu ra mà model
đang stream dở.

### Việc tái tạo phiên bị chặn

`set_live_active(True)` chặn mọi lần tái tạo phiên sau lượt trong
`stream_output()` suốt thời gian phiên, vì cả ba lý do đều sai bên trong một
phiên:

- **zombie** — một quãng im lặng không sinh đầu ra, nên
  `REALTIME_ZOMBIE_RECONNECT_AFTER` lần như vậy (~24 s người dùng chỉ đơn giản là
  không nói) sẽ ép kết nối lại giữa cuộc trò chuyện.
- **turn-cap** — đổi phiên sau mỗi `REALTIME_SESSION_MAX_TURNS` câu trả lời.
- **idle** — cùng việc đổi phiên, cùng một đường lên đang mở.

Việc dựng lại ở đây cũng không sống sót được như giữa các lượt: bơm mic vẫn tiếp
tục append xuyên qua lúc đổi. Hoãn lại chứ không hủy — `set_live_active(False)`
reset các bộ đếm khi cúp máy.

### Chế độ live không làm gì

Mọi thứ dưới đây gắn với ranh giới lượt và không có nguồn cung trong một phiên
live. Đây là đánh đổi sản phẩm, không phải lỗi:

| mất | hệ quả |
|---|---|
| transcript STT | không có `[TURN CONTEXT]`, không có bộ lọc dựa trên transcript |
| wake word | được xác nhận từ text STT, nên `HAL_WAKEWORD_ENABLED` không có tác dụng ở chế độ live — vào phiên chỉ bằng VAD |
| speaker ID, cảm xúc giọng nói | một phiên không tạo ra cả hai |
| STT cục bộ khi delegate | `delegate_to_main` kết thúc phiên và chuyển tiếp `[voice-instruction]` + transcript đầu vào của chính Gemini làm `[transcript]` (`FunctionCallOutput.user_transcript` → `DelegateSignal.transcript`, `_live_out_pump` đọc); câu trả lời của main agent phát sau khi cúp máy. Provider không có input transcription chỉ chuyển tiếp instruction |

Cũng không chạy bên trong một phiên: cổng RMS vào, `SPEECH_HOLDOFF_S`, đồng hồ im
lặng, `MAX_SESSION_DURATION_S`, socket STT mỗi lượt và keepalive của nó (thậm chí
không được kết nối trước — `stt_keepalive_on` là false ở chế độ live), noise
guard, warm-mic drain và echo-skip, và `commit_audio`. Không cái
nào bị xóa: đường lượt vẫn dùng tất cả, và lấy lại chúng ngay khi một phiên kết
thúc.

### Cấu hình

| Env | Mặc định | Ý nghĩa |
|-----|----------|---------|
| `HAL_LIVE_MODE` | `false` | Chế độ live cho toàn tiến trình. Ép `HAL_REALTIME_TURN_DETECTION=server_vad` khi giá trị đó là `off` |
| `HAL_LIVE_UPLINK_DURING_PLAYBACK` | `mute` | `mute` (không cắt lời, dùng được ngay) hoặc `cancelled` (song công thật, cần sửa AEC) |
| `HAL_LIVE_PLAYBACK_TAIL_S` | `0.35` | Đuôi âm học sau lần ghi reference cuối, trong đó phòng vẫn được tính là đang phát |
| `HAL_LIVE_IDLE_HANGUP_S` | `15` | Cúp máy sau khoảng này khi **người dùng** không có hành động nào, tính từ mốc muộn hơn: lời cuối của người dùng hoặc thời điểm thiết bị nói xong |
| `HAL_LIVE_MAX_UNPROMPTED_REPLIES` | `3` | Trần cứng cho số câu trả lời liên tiếp của model mà không có tiếng người dùng xen giữa — cắt vòng lặp tự nói mà không cắt ngang một câu trả lời dài |
| `HAL_LIVE_MAX_S` | `600` | Trần tuyệt đối cho một phiên |

### Hạn chế đã biết: `mute` có thể tự kích hoạt

Với `mute`, đường lên mang **im lặng số** trong toàn bộ cửa sổ phát và mang âm
thanh phòng thật sau đó. Chuyển tiếp đó là một điểm bắt đầu về biên độ, và VAD
phía server đọc một điểm bắt đầu thành có người bắt đầu nói — nên thiết bị có
thể tự trả lời *chính nó*. Quan sát trên thiết bị 07/09/2026 tại
`intern-v2-6286` ở mức âm lượng 70 %: bốn câu trả lời không ai hỏi trong 35 s
khi không có ai trong phòng ("What's up?", "I'm here. What can I do for you?"),
mỗi câu lại làm mới thời gian giữ K, và một dòng log
`barge-in: model interrupted by the user` trong khi không có người dùng nào.

`HAL_LIVE_MAX_UNPROMPTED_REPLIES` giới hạn thiệt hại — nó kết thúc phiên khi model
đã nói liên tiếp bấy nhiêu câu mà không có gì từ người dùng — nhưng đó là chốt
chặn, không phải cách chữa. Nó đếm số câu trả lời thay vì số giây chính là để một
câu trả lời dài vài phút không bao giờ bị nhầm thành vòng lặp. Cách chữa là chế độ `cancelled` trên một bộ khử vọng đủ tốt, để model
luôn nghe căn phòng thật mà không có chuyển tiếp nhân tạo. Giảm âm lượng loa làm
giảm rõ rệt khả năng xảy ra trong lúc chờ.

### Kết thúc một phiên

Một phiên kết thúc sau `HAL_LIVE_IDLE_HANGUP_S` (K, mặc định 15 s) mà **người
dùng không có hành động nào**, và mic được trả thẳng lại cho VAD, vốn sẽ mở phiên
mới khi có tiếng nói thật tiếp theo. Hai chi tiết khiến việc này chạy đúng:

- **Câu trả lời của model không phải hành động của người dùng**, nhưng đồng hồ
  được *giữ* trong lúc thiết bị đang nói, nên một câu trả lời dài không bao giờ
  bị cắt giữa chừng. Cửa sổ đếm từ mốc muộn hơn trong hai mốc: lời cuối của người
  dùng, hoặc thời điểm thiết bị ngừng nói — đúng lúc lượt thuộc về người dùng.
- **Chỉ RMS thì không gánh nổi đồng hồ này.** Trong phòng ồn, sàn nhiễu nằm trên
  `HAL_VAD_THRESHOLD`, nên mọi khung đều đọc thành "người dùng đang nói" và phiên
  không bao giờ cúp. Quan sát trên thiết bị 07/09/2026 tại `intern-v2-6286`: sàn
  nhiễu ~10500 so với ngưỡng 500 đã giữ một phiên mở vô hạn, và vì phiên live sở
  hữu mic, VAD không bao giờ chạy lại — thiết bị điếc cho tới khi khởi động lại.
  Vì vậy RMS là cửa đầu tiên rẻ tiền và **Silero xác nhận** trước khi đồng hồ
  được làm mới, gom theo `HAL_SILENCE_VAD_WINDOW_FRAMES`, đúng như đồng hồ im
  lặng của đường lượt.

`HAL_LIVE_MAX_S` là chốt chặn cuối cho căn phòng ồn tới mức ngay cả Silero cũng
liên tục đồng ý.

Đọc các bộ đếm ở dòng log session-END: `substituted` ở mức ~100 % của
`during_playback` là `mute` đang hoạt động đúng thiết kế.

## Luồng một lượt (trong `voice_service.py`)

1. **Dựng + start.** `RealtimeOrchestrator(gateway=AGENT_GATEWAY)` được tạo;
   `start()` chạy trong daemon thread (`realtime-start`) khi `HAL_REALTIME_ENABLED`.
   TTS `on_speak_end` được hook để feed lại text đã nói dạng `[TTS HISTORY]`,
   nhưng **chỉ khi lượt nói đó opt-in** (`TTSService.realtime_feedback`, đặt bởi
   cờ `realtime_feedback` trên `/voice/speak[-queue]`). Chỉ reply thật của agentic
   runtime mới opt-in — os-server gửi qua `hal.SpeakReply` / `hal.SpeakQueueReply`
   (được `SendToHALTTS` / `SendToHALTTSQueue` dùng). Mọi TTS hardcode (dead-air
   filler, ambient mumble, backchannel, thông báo reconnect/health, chitchat
   local) đi qua `hal.Speak` thường và **không bao giờ** được feed lại — nếu không
   model sẽ lặp lại (echo) những câu nó chưa từng sinh ra.
2. **Stream.** Khi session STT đang mở, mỗi frame mic được resample về rate của
   provider và gửi qua `append_audio()` (song song, non-blocking), đồng thời buffer
   vào `rt_audio_buffer`.
   Khi bật STT keepalive tùy chọn, nếu socket STT pre-connect đóng bình thường
   (WS 1000) đúng lúc bắt đầu nói, HAL thay socket trước khi stream tiếp và replay
   toàn bộ pre-roll đúng một lần vào socket mới. Nhờ vậy không mất từ mở đầu; close
   bình thường đã recovery là warning, không phải error.
   Gemini Manual VAD không có lệnh huỷ một activity đã stream. Vì vậy turn
   empty-STT/noise khởi động session sạch thay vì để noise lẫn vào câu người dùng
   kế tiếp. Reconnect này chạy nền: nếu user nói ngay, HAL giữ toàn bộ audio của
   turn mới ở local rồi gửi đúng một lần, đúng thứ tự khi session thay thế sẵn
   sàng. Nếu reconnect chậm/lỗi thì fallback về main agent với transcript STT;
   không làm rớt audio đầu câu hoặc commit nó vào activity cũ.
3. **Bơm turn context + prepass speaker-ID.** `[TURN CONTEXT]` (thời gian, nhắc
   ngôn ngữ trả lời, user hiện tại) được gửi dạng text không tạo response. **User
   hiện tại chính là người nói (VOICE speaker)** được nhận dạng trong lượt này — nó
   **ghi đè** `current_user` suy ra từ khuôn mặt, và rơi về định danh khuôn mặt khi
   không có voice ID (unknown / gate-reject / không có transcript).

   **Thời điểm chạy khác nhau theo mode**, vì voiceprint cần trọn câu nói nên không
   thể tồn tại lúc mới mở session:

   | Mode | Gửi `[TURN CONTEXT]` | Biết người nói? |
   |------|---------------------|-----------------|
   | Always-listening (`wakeword=false`) | lúc **mở** session, trước khi có audio | Không → fallback khuôn mặt, rồi mới sửa |
   | Wake-word / follow-up | sau khi capture xong, khi final xác nhận wake phrase | Có |
   | Deferred (rebuild sau noise-drop) | sau khi capture xong, trên session thay thế | Có |

   Cả hai dòng chạy sau capture còn bị chặn thêm một điều kiện: lượt đó **không**
   phải noise. Chúng chạy sau khi noise guard đã phân loại xong capture, nên một
   lượt STT rỗng mà không phải tiếng nói sẽ không mở gì cả: không `[TURN CONTEXT]`,
   không audio, không session thay thế. Chính việc không gửi mới làm cho đường
   skip-commit trở nên miễn phí — nếu không, toàn bộ buffer của lượt đó đã vào (và
   bị tính tiền trong) một activity đang mở mà ngay bước sau lại vứt đi. Session
   được mở *sớm hơn* trong lúc capture (always-listening) thì đã stream audio rồi
   nên vẫn bị discard như cũ.

   Ở mode always-listening, prepass speaker-ID (`identify_and_decorate`, chạy **một
   lần** cuối session) chỉ giải được người nói *sau khi* context đã gửi đi kèm tên
   từ khuôn mặt. HAL gửi tiếp một correction `[TURN CONTEXT UPDATE]` nêu đúng người
   nói — vẫn **trước** `commit_audio()` nên thuộc cùng một lượt. Transcript ngắn
   trong vùng mơ hồ AI-rejection sẽ hoãn external embedding call tới khi realtime
   quyết định xong; một lần reject rõ ràng tránh luôn call này, còn mọi turn không
   reject vẫn nhận cùng một kết quả identity duy nhất trước khi đi hạ nguồn. Bỏ qua
   khi context đã mang đúng tên, hoặc khi lượt đó là noise.

   **Prepass không còn chặn model.** Trước đây nó chạy nội tuyến, ngay trước khi
   mở lượt realtime, nên trọn vòng gọi ra ngoài nằm giữa lúc user dứt lời và lúc
   model nhận được câu nói — đo trên lamp-0c89 (03/09/2026): 1.49s trong khoảng
   trống 3.0s, phần còn lại là cú reconnect Gemini trước turn. Giờ nó chạy trên
   thread riêng song song với cú reconnect đó, và lượt nói chỉ join lại
   (`SPEAKER_PREPASS_JOIN_S`, `HAL_SPEAKER_PREPASS_JOIN_S`, mặc định 2.0s) ở đúng
   chỗ đầu tiên cần tới tên người nói. Thời gian chờ là **trần**, không phải độ
   trễ: prepass xong trong lúc reconnect thì không tốn gì, còn chạm trần chỉ có
   nghĩa là context lượt này gửi đi khi chưa biết người nói — đúng bằng những gì
   dòng always-listening ở trên vẫn làm, và correction `[TURN CONTEXT UPDATE]` vẫn
   phủ được. Đường deferred cho transcript ngắn giữ nguyên.

   **Kết quả được cache.** Trước đây nhận dạng chạy mỗi lượt, lượt nào cũng chạy:
   một cuộc mười lượt trả tiền mười lần gọi ra ngoài để nghe đúng một cái tên.
   `SpeakerDecorator` giờ dùng lại kết quả gần nhất trong `SPEAKER_ID_CACHE_S`
   (`HAL_SPEAKER_ID_CACHE_S`, mặc định 90s), và `SPEAKER_ID_CACHE_FOLLOWUP_S`
   (mặc định 300s) khi đang trong cửa sổ follow-up của wake word — những lượt đó
   theo định nghĩa là cùng một cuộc trò chuyện. **Unknown cũng được cache** — câu
   mà recognizer không xếp được chính là ca dễ lặp lại nhất, thử lại mỗi lượt là
   trả trọn độ trễ để nhận cùng một câu trả lời rỗng; thứ duy nhất mất khi cache
   unknown là đường dẫn WAV phục vụ enrol của lượt đó. `POST
   /speaker/current-user/reset` xoá luôn cache cùng với voice user hiện tại, vì
   đó là cùng một trạng thái presence ở tầng dưới.

   **Session bị thay được đóng ở nền.** Rebuild trước turn sẽ dựng session mới
   rồi dọn session cũ; làm inline thì lượt nói phải đợi `aclose()` cộng cú join
   IO thread — đo được 0.79s giữa lúc session mới mở và lúc `[TURN CONTEXT]` của
   lượt này bay đi (lamp-0c89, 03/09/2026). Không ai cần socket cũ đóng xong thì
   model mới nghe được người dùng, nên việc đóng chạy trên thread riêng (lỗi vẫn
   được log; nếu không tạo nổi thread thì đóng inline chứ không rò socket).

   **Lưu ý Gemini native-audio:** `send_text()` bỏ **toàn bộ** text không tạo
   response trên các model Gemini `*native-audio*` (`gemini_needs_idle_workaround()`),
   vì các message SDK `clientContent(turn_complete=False)` lặp lại va với lượt audio
   sau đó và đóng WS 1011. Trên các model đó cả context lẫn correction đều không tới
   được câu trả lời, và model rơi về định danh còn lưu trong memory của session.
   `gemini-3.1-flash-live` và OpenAI thì nhận cả hai. Mọi lần bỏ đều được log
   (`[realtime->model] DROPPED …`).

   **Điều này không áp dụng cho cấu hình mặc định đang ship.** `REALTIME_GEMINI_MODEL`
   mặc định là `gemini-3.1-flash-live-preview` (`hal/config.py:734`), không phải
   native-audio, nên guard tắt và cả context lẫn correction đều tới được model. Nó chỉ
   bật lại khi ai đó cấu hình một model `*native-audio*`.
4. **Commit.** Cuối session, nếu enabled + `available` + có audio buffer, gọi
   `commit_audio()`. Cue emotion `thinking` fire cùng lúc commit (mặt + servo +
   LED pulse ÉP HIỆN — `thinking` vốn là background emotion có LED nhường
   màu user đã set; cue realtime bypass đúng guard đó, còn user tắt đèn thì vẫn
   tắt) và được clear về `idle` khi có output đầu tiên (câu TTS đầu hoặc frame
   audio native đầu) hoặc khi turn chết không output — trừ khi model đã tự
   express emotion riêng. Lấp khoảng 1-3s latency của model mà trước đây device
   nhìn như đứng hình.

   Cùng lúc commit cũng arm **dead-air filler** (`_WaitFiller`) — nửa phần tiếng
   của chính cue đó. Mặc định (`HAL_REALTIME_FIRST_CHUNK_MAX_CHARS=0`), câu
   hoàn chỉnh đầu tiên được nói ngay; các câu sau vẫn được tổng hợp trước qua
   hàng đợi, không chờ toàn bộ câu trả lời. Giá trị dương bật tùy chọn cắt câu
   đầu chưa hoàn chỉnh tại ranh giới mệnh đề cuối (`,` `;` `:` `—`), hoặc tại
   khoảng trắng khi vượt giới hạn ký tự. Câu hoàn chỉnh bỏ qua bộ cắt sớm này.
   Bộ cắt giữ nguyên voice tag trong ngoặc vuông, kể cả khi cắt theo khoảng
   trắng; không cắt tại dấu phẩy/hai chấm trong số hoặc dấu hai chấm của URL.
   Phần được cắt cần ít nhất 8 ký tự hiển thị ngoài tag. Cắt sớm có thể giảm
   thời gian chờ ban đầu nhưng vẫn tạo khoảng ngắt giữa các yêu cầu tổng hợp,
   nên chỉ bật khi chấp nhận đánh đổi này. Native audio vẫn stream từng frame.
   Sau `HAL_REALTIME_FILLER_DELAY_S` (mặc định 1.5s) mà vẫn
   chưa có output nào, HAL gọi `POST /api/sensing/filler` và os-server phát một
   filler realtime riêng từ cache — tiếng đệm suy nghĩ không lời như "Ừm...",
   khác với lời xác nhận mở đầu của main agent. Pool phrase, ngôn ngữ và WAV
   cache đều nằm ở os-server. Filler bắn ở mọi lượt hay chỉ ở lượt chậm là
   **tính chất của model**, và giá
   trị mặc định giả định model nhanh: câu chit-chat về trong ~1s thì không chạm
   timer, còn lượt dùng Google Search thì có. Phải ĐO trước khi tin điều đó trên
   một body cụ thể — trên `lamp-0c89` (26/08/2026, `gemini-3.1-flash-live-preview`
   qua proxy campaign-api) không lượt nào ra câu đầu dưới 3.0s (median 4.0s,
   n=31), nên filler là thứ duy nhất người dùng nghe được lúc đầu, và lamp hạ
   ngưỡng xuống 0.5s trong `.env` của nó. Đặt giá trị này theo thời gian
   time-to-first-sentence đo được, đừng theo mặc định. Filler
   **không được arm cho transcript ngắn nằm trong vùng mơ hồ của noise guard**
   (tối đa `HAL_REALTIME_NOISE_GUARD_MAX_WORDS`, mặc định 3 từ): model có thể
   `reject_turn` rõ ràng cho `o`, `you.` hay `Yeah.` ngay sau commit, và filler
   sớm sẽ biến một lần từ chối im lặng thành âm thanh gây khó chịu. Filler phát
   interruptible nên câu đầu tiên của model cắt ngang nó; mọi đường thoát
   (trả lời, delegate, turn rỗng, exception) đều cancel timer, riêng delegate
   cancel tường minh vì chặng main agent ngay sau đó tự bắn filler của nó. `0`
   để tắt.

   Phát filler là TTS, nên nó dừng pulse thinking và chạy speaking wave. Để
   phần chờ còn lại vẫn có tín hiệu, cue đánh dấu strip là của mình
   (`app_state._thinking_cue_active`): lần restore LED sau TTS vẽ lại pulse
   thinking thay vì rơi về user state. Cờ được bỏ khi cue clear và khi có bất
   kỳ emotion nào khác vào qua `POST /emotion`, nên emotion model tự express
   không bị đè.

   Turn **delegate** cố ý giữ cue — chặng main-agent phía sau mới là phần chờ
   dài, và hook của nó cũng tự bắn `thinking` lại. Turn ném exception thì KHÔNG
   phải bàn giao đó (không còn gì trong HAL đang lái mặt), nên nhánh exception
   clear cue trước khi rơi xuống forward sang OS server.

   Vì `thinking` chỉ bị kết thúc bởi chính emotion mà câu trả lời express, một
   turn không sinh ra emotion nào — delegate mà agent trả lời không kèm marker,
   forward không bao giờ xảy ra — từng để mặt (và qua `_thinking_cue_active`,
   mọi lần restore LED sau đó) kẹt ở pulse cho tới khi user nói tiếp. Giờ có hai
   thứ kết thúc nó.

   **Câu trả lời nói xong = hết chờ.** `_on_tts_speak_end` (`hal/app_state.py`)
   clear `thinking` khi TTS kết thúc, có gate `tts_service.realtime_feedback` —
   cờ chỉ do chính reply của agentic runtime set. Dead-air filler, mumble,
   system notice để False, nên TTS phát *trong lúc* chờ (đúng thứ mà cờ cue sinh
   ra để sống sót qua) không kết thúc cue. Đây là ca phổ biến và được xử đúng
   thời điểm: mặt đúng ngay khi máy ngừng nói, bất kể agent có nhả marker hay
   không.

   **Watchdog là lưới cho turn không hề nói.** `POST /emotion` arm một timer
   chặn cuối mỗi khi emotion là `thinking`: sau
   `HAL_EMOTION_THINKING_RESET_S` (mặc định 25s, `0` = tắt) thinking LIÊN TỤC,
   nó bỏ cờ cue, express `idle` và restore LED user state. Bất kỳ emotion nào
   khác huỷ timer; một `thinking` mới arm lại. Cửa sổ này lớn hơn khoảng giữ
   thật dài nhất đo trên máy (realtime clear trong 0.4-8.6s; delegate
   event-forwarded → assistant-turn-done chạy 6-22s), nên không thể nháy idle
   giữa lúc turn còn sống.
5. **Tiêu thụ.** `for output in stream_output()`:
   - `TextOutput` → các câu được flush sang TTS (`speak` / `speak_queue`).
     Nếu `speak` báo busy (TTS khác đang giữ loa non-interruptible, ví dụ
     nudge ambient), câu sẽ fallback sang `speak_queue` để phát sau đó thay
     vì bị mất luôn.
     Speech của agent trong queue nhận biết theo lượt: mỗi entry mang `turn_id`
     và `turn_seq` tăng đơn điệu.
     Khi nhận một run mới hơn, TTS dừng câu cũ đang phát và bỏ các entry
     pending của run cũ để người dùng nghe reply phù hợp ở thời điểm đó. Một
     request đến muộn từ run đã bị thay thế cũng bị bỏ thay vì quay lại queue.
     `POST /tts/stop` cũng huỷ cả playback đang chạy lẫn mọi entry pending.
   - `DelegateSignal` → dừng; chuyển `[voice-instruction] …` + transcript tới OS
     server với `event_type` gốc.
   - Ngược lại lượt đã được xử lý cục bộ → báo OS server `voice_agent_handled`
     (để OpenClaw trả `NO_REPLY`, bỏ filler dead-air), và lưu lượt vào realtime memory.

## Cấu hình

Realtime agent được cấu hình từ **block `realtime` trong `config.json`** của thiết
bị (các knob hướng người vận hành), với biến môi trường `HAL_*` của HAL là override
cho dev và default built-in là sàn. Thứ tự ưu tiên mỗi knob:

```
biến HAL_*  >  block "realtime" trong config.json  >  default built-in
```

os-server **seed** block này vào `config.json` lúc start lần đầu — và khi upgrade
nếu thiếu — nên file luôn có realtime config sửa được. HAL **tự đọc** trực tiếp
(giống `llm_api_key` / `stt_language`), không push xuống. Vì HAL đọc `config.json`
lúc import, đổi config phải **restart HAL** mới ăn. Sửa lúc đang chạy thì restart
liền (`restartHAL` trong `system/device/service.go`).

### Model và ngôn ngữ STT

`stt_language` chọn `stt_model` được lưu: English dùng `flux-general-en`; tiếng
Việt và các ngôn ngữ không phải English được hỗ trợ dùng `nova-3-general` với mã
BCP-47 đã chọn. Cặp đó được truyền cho proxy AutonomousSTT, kể cả lúc healthwatch
khởi động lại voice pipeline. Vì vậy cấu hình tiếng Việt đã lưu vẫn có hiệu lực
sau khi proxy restart; điều này không có nghĩa một model duy nhất xử lý chính xác
mọi trường hợp code-switching Việt–Anh.

**Chỉ restart khi config thực sự đổi.** os-server *không* restart HAL mỗi lần
os-server restart — làm vậy sẽ rớt voice pipeline vô ích. Thay vào đó nó hash
`config.json` và lưu hash vào `config/.hal_config_hash` mỗi khi (re)start HAL. Lúc
boot (`handleSetUpCompleteChange` trong `server/config_watch.go`) nó chỉ restart HAL
khi hash hiện tại khác snapshot — tức config thật sự đổi trong lúc os-server tắt
(setup mới, OTA đổi config, sửa lúc downtime), hoặc chưa có snapshot (boot đầu). Một
lần os-server restart bình thường với config không đổi sẽ để nguyên HAL đang chạy.
Nếu HAL thật sự chết, `hal.service` (`Restart=always`, `RestartSec=5`) tự hồi độc
lập, nên skip restart là an toàn. Nhánh `restartHAL` cập nhật lại snapshot sau khi
restart HAL, nên đổi lúc-chạy rồi os-server restart không restart hai lần. Hash cả
file (thay vì chỉ tập field HAL đọc) giữ tín hiệu tự-bảo-trì khi tập field HAL đọc
thay đổi; cái giá duy nhất là một lần restart HAL thừa ở boot kế tiếp khi sửa field
chỉ-thuộc-os-server.

### Block `realtime` trong `config.json`

Model ở Go tại `system/server/config/realtime.go`; đọc ở HAL tại
`hal/config.py`. Field chung ở trên; knob theo provider nằm trong sub-object
`gemini` / `openai` / `qwen`, `provider` chọn cái đang active (`none` hoặc vắng →
tắt realtime). `api_key` / `base_url` rỗng → fallback `llm_api_key` /
`llm_base_url` — **trừ qwen**: credential của qwen là của riêng nó
(`realtime.qwen.api_key` / `realtime.qwen.base_url`, Go struct `QwenRealtime`
còn có `model`/`voice`), **cố tình không** fallback về `realtime.api_key`/
`base_url` chung hay credential `llm_*` — qwen nói thẳng với host Alibaba MaaS,
không đi qua proxy `campaign-api`. Set qua `realtime.qwen.*` trong config.json
hoặc qua env trên device (`DASHSCOPE_API_KEY`, `HAL_QWEN_REALTIME_BASE_URL`
trong `/opt/hal/.env`); thiếu cả hai thì WS handshake fail rõ ràng trong log hal.

> **Để `base_url` trống trừ khi có endpoint riêng (không qua proxy).** Khi trống,
> HAL tự suy ra `<llm_base_url>/ws/gemini` (hoặc `/ws/openai`) — đúng suffix WS mà
> proxy `campaign-api` route. Nếu `base_url` bị set bằng `llm_base_url` trần (thiếu
> `/ws/...`), giá trị đó được đưa thẳng vào SDK provider và **404 ngay ở Live
> handshake**. Vì vậy ô "Base URL" trong web Settings chỉ hiển thị *override tường
> minh* (`RealtimeBaseURLOverride`, không phải giá trị đã resolve), để "để trống là
> tự suy ra" luôn trống và mỗi lần Save không vô tình ghi đè URL trần. Quy tắc
> này KHÔNG áp cho qwen: qwen giữ `base_url` riêng trong sub-object của nó và
> không bao giờ suy ra từ `llm_base_url`.

```json
{
  "wakeword": false,
  "realtime": {
    "enabled": true,
    "provider": "gemini",
    "gemini": { "model": "gemini-3.1-flash-live-preview", "voice": "Kore", "thinking_level": "MINIMAL" },
    "openai": { "model": "gpt-realtime-2", "voice": "alloy", "reasoning_effort": "minimal" },
    "qwen": { "api_key": "sk-…", "base_url": "wss://…", "model": "qwen3.5-omni-plus-realtime", "voice": "Ethan" }
  }
}
```

Knob reasoning (`thinking_level` / `reasoning_effort`) default về mức **rẻ nhất**
(`MINIMAL` / `minimal`), không phải mức max của provider — muốn reasoning sâu hơn
thì set tường minh. Qwen **không có** knob reasoning/thinking, nên web ẩn
selector Reasoning khi provider là qwen. Các knob KHÔNG có trong block (turn
detection, session resumption, memory, summarizer) vẫn chỉ theo env/default.

**Filter chống leak CoT.** Trên `gemini-3.1-flash-live-preview` KHÔNG tắt được
thinking: `thinking_level=MINIMAL` lẫn `thinking_budget=0` đều được chấp nhận
nhưng bị bỏ qua (đo `thoughts_token_count` 125–168 trên turn cần suy luận với mọi
config). Bình thường thoughts nằm nội bộ, nhưng trên các turn có
grounding/vision/tool, server thỉnh thoảng đổ nguyên text channel của model —
đoạn lập kế hoạch tiếng Anh ("The user is insisting…", "Phrasing draft:",
"Delivery guidance:") kèm câu trả lời thật — vào `output_audio_transcription`,
trong khi audio của model chỉ chứa câu trả lời sạch. Vì native audio tắt, HAL
đọc transcription → không có guard thì leak bị đọc thành tiếng (tốn ký tự TTS)
và forward vào `[REPLY]`, quay lại context và tự củng cố.
`drivers/voice/_internal/cot_leak_filter.py` chặn leak ở mức câu, trước TTS và
trước khi transcript được forward/lưu, theo 3 tầng: marker TRIGGER (ngôi thứ ba
gắn động từ "the user is/wants…", nhãn planning như "Phrasing draft:") luôn drop
và bật cot-mode cho turn; marker PHỤ ("persona", "system prompt", "emotion
tool", …) chỉ drop khi cot-mode đã bật — câu trả lời hợp lệ nói về chính thiết
bị vẫn an toàn; trong cot-mode drop thêm câu planning tiếng Anh (chỉ với device
không nói tiếng Anh — chữ viết không-Latin như tiếng Việt/Trung/Nhật dùng check
tỉ lệ ASCII, chữ Latin như Pháp/Indo yêu cầu thêm function word tiếng Anh để
answer thật không bị nuốt), draft trong ngoặc kép, mảnh plan vụn, và câu
gần-trùng câu đã giữ (CJK token theo từng ký tự). Check ngôn ngữ bỏ qua các
đoạn nằm trong ngoặc, nên câu planning tiếng Anh nhúng text ngôn-ngữ-trả-lời
trong ngoặc ("The search query 'cách dùng…' didn't yield…") vẫn bị bắt, còn
câu ngôn-ngữ-trả-lời trích dẫn tiếng Anh thì không. Mỗi câu bị drop đều log
`CoT leak dropped`.

Đường agent chính (reply openclaw/hermes nói qua os-server) có bản port Go của
filter này — `system/server/agent/delivery/http/cot_leak_filter.go` (thêm
TRIGGER identifier snake_case cho corpus leak DeepSeek); xem
`docs/vi/flow-monitor_vi.md` § "CoT-leak filter (đường agent)". Harden bên nào
thì nhớ sync bên kia.

### Cấu hình runtime (`hal/config.py` + `config.json`)

Mỗi biến môi trường `HAL_*` ghi đè setting tương ứng; `wakeword` là cờ top-level
trong `config.json`:

| Biến | Mặc định | Ghi chú |
|------|----------|---------|
| `HAL_REALTIME_ENABLED` | `true` | Cổng tổng cho pipeline realtime |
| `wakeword` | `voice.wakeword` trong ROBOT.md khi config còn mới, ngược lại `false` | Cổng wake word top-level trong config file. Khi bật, partial khớp chỉ là tín hiệu tạm: HAL chỉ commit audio buffer sang realtime hoặc forward command sau khi STT **final** xác nhận wake phrase. Transcript được tách thành câu (`.` `!` `?`) và phrase được chấp nhận ở đầu **hoặc cuối** bất kỳ câu nào; xuất hiện giữa câu bị từ chối. Bước xác nhận kiểm lại trên transcript đã ghép mà vẫn còn dấu câu, để bước merge chỉ giữ `\w+` không rút lại cái gate mà một partial đã mở. Nếu bước kiểm khớp tuyệt đối đó trượt nhưng trước đó đã có một partial khớp chính xác, thì riêng chữ TÊN được phép lệch 1 ký tự và gate vẫn được xác nhận: STT tự viết lại giả thuyết của nó ở final, và trên lamp-0c89 (04/09/2026) partial `hello lamp` quay lại thành `Hello, lamb.` làm rơi cả lượt — không mở lượt realtime, không có cue thinking, câu hỏi rơi xuống main agent chậm hơn nhiều. Tiền tố (`hello`, `hey`, …) vẫn phải khớp tuyệt đối, và luật lỏng này KHÔNG BAO GIỜ mở được gate mà chỉ xác nhận lại gate do một partial khớp chính xác đã mở, nên một từ gần giống trong lời nói xung quanh vẫn không đánh thức được gì. Nó được log riêng thành `Wake-word confirmed with a one-letter STT slip` để còn đếm được — nhiều dòng này nghĩa là keyterm boost đang không làm tròn việc. Các prefix hỗ trợ là `hello`, `hey`, `hi`, `alo`, `okay`, `ok`, `wake up`, áp dụng cho alias chung cố định (`hey autonomous`), device type (`hey lamp`) và tên agent hiện tại (`hey Luna`). Runtime rename chỉ cập nhật alias theo tên agent. Bare name và các prefix khác không mở gate. Một câu bị từ chối sẽ bị bỏ và LED `listening` tạm thời được restore về trạng thái nghỉ bình thường; không bao giờ để hiệu ứng `idle` cố định tiếp tục chạy. Một lượt đã xác nhận mở cửa sổ focus follow-up; lượt trong cửa sổ đó được forward dưới type `voice_followup` mà không cần wake phrase khác. Mọi lượt được phép đều dispatch sang os-server: câu realtime đã nói thành event đồng bộ im lặng `voice_agent_handled`; realtime unavailable, im lặng, lỗi hoặc delegate đi theo đường thường. Nếu realtime tắt, final transcript đã xác nhận đi theo đường os-server thường. Thiếu/`false` giữ nguyên luồng luôn lắng nghe trước gate. Với `config.json` do os-server tạo ra, giá trị khởi tạo lấy từ `voice.wakeword` của body (xem phần Cổng wake word ở trên); config nạp lên mà không có key thì vẫn là `false`. HAL restart sau khi lưu ở local Settings hoặc MQTT `wakeword.gate`. |
| `HAL_WAKEWORD_FOLLOWUP_TIMEOUT_S` | `20` | Số giây idle của cửa sổ focus sau lệnh. Mỗi `voice_command` hoặc `voice_followup` được nhận sẽ refresh cửa sổ. `0` tắt follow-up và buộc mỗi phiên mic phải có wake phrase. Bị bỏ qua khi `wakeword` là false. |
| `HAL_ENDPOINT_SILENCE_S` | `0.8` | Thời gian im lặng từ lúc STT final về, chỉ áp dụng khi `final_ts >= last_confirmed_speech`. Nếu có tiếng nói được xác nhận sau final đó, quay lại ngưỡng dự phòng 2.5s tới khi có final mới. `0` tắt đồng hồ ngắn, chỉ dùng `HAL_SILENCE_TIMEOUT`. |
| `HAL_SILENCE_VAD_ENABLED` | `true` | Yêu cầu Silero xác nhận có tiếng nói trước khi refresh đồng hồ im lặng kết thúc lượt. RMS vẫn là cổng chặn rẻ chạy trước; đặt `false` để quay về phát hiện im lặng thuần RMS. |
| `HAL_SILENCE_VAD_WINDOW_FRAMES` | `3` | Số frame gom lại cho mỗi lần chạy Silero ở bước kiểm đó — Silero tốn ~20 ms/frame trên ARM và LSTM của nó cần hơn một frame 64 ms mới ổn định. |
| `HAL_REALTIME_PROVIDER` | `gemini` | `none` \| `gemini` \| `openai` \| `qwen` |
| `HAL_REALTIME_TURN_DETECTION` | `off` | `server_vad` \| `semantic_vad` \| `off` (Gemini: off = activity detection thủ công) |
| `HAL_REALTIME_RECV_QUEUE_TIMEOUT_S` | `8.0` | Số giây tối đa `receive()` chờ output event kế tiếp trước khi kết thúc lượt im lặng (fallback sang main agent) |
| `HAL_REALTIME_GROUNDING_DEBUG` | `false` | In toàn bộ field của `grounding_metadata` từ Gemini, mỗi lượt có grounding một lần (`grounding_chunks`, `grounding_supports`, `search_entry_point`, …). Chỉ để chẩn đoán và rất dài dòng; nó sinh ra để phân biệt lượt mà search thật sự không trả về gì với lượt bị cắt payload trên đường truyền. Đo trên lamp-0c89 04/09/2026 qua bốn lượt có grounding, payload luôn về đủ — nên `chunks=0` nghĩa là model không dùng nguồn nào cho câu trả lời đó. |
| `HAL_REALTIME_TURN_MAX_SILENCE_S` | `20.0` | Trần thời gian một lượt được im lặng khi server vẫn còn gửi message. `receive()` chỉ kéo dài quá `HAL_REALTIME_RECV_QUEUE_TIMEOUT_S` khi lưu lượng vào chứng minh model còn đang làm việc (search grounding không phát output tới khi xong); trần này chặn trường hợp server nói liên tục mà không bao giờ ra output. `0` tắt cơ chế giữ lượt, quay về watchdog gap thuần. |
| `HAL_REALTIME_LOOK_RECV_TIMEOUT_S` | `20.0` | Watchdog im-lặng dùng thay mặc định cho turn có `look` (theo từng turn, qua `extend_recv_timeout()`). Gemini bị ép thinking trên frame dày chữ có thể im >8 s ngay trước khi trả lời — watchdog mặc định giết nhầm mấy turn đó. Nâng nó lên là hoãn luôn handoff frame `look`, nên phải giữ `HAL_GEMINI_VISION_HANDOFF_MAX_AGE_S` cao hơn |
| `HAL_REALTIME_REQUIRE_TRANSCRIPT` | `true` | Không bao giờ commit turn empty-STT lên model. Final transcript chỉ có dấu câu/ký hiệu (ví dụ `.`) được chuẩn hoá thành empty trước gaze, speaker-ID, realtime, dispatch hay refresh follow-up; nó không thể tạo `voice_followup`. Giọng thật mà nova-3 miss (câu ngắn) vẫn là voiced nên qua hết guard VAD/Silero, commit audio thô khiến model bịa câu trả lời cho khoảng im lặng (lời chào chung chung, thường kèm tên không ai nói). Khi `true`, mọi turn empty-STT bị bỏ bất kể duration/voicing — im còn hơn trả lời sai. Đặt `false` để quay về đường audio-only gated bằng Silero bên dưới. |
| `HAL_REALTIME_AI_REJECT_FILTER` | `true` | Đăng ký `reject_turn` và bật policy gate tách riêng `should_drop_realtime_rejection()`. Tool call rõ ràng sẽ bỏ transcript trước OS dispatch; model im lặng, timeout hay lỗi vẫn fallback sang main agent. Noise guard deterministic riêng cũng terminal cho audio mà nó đã phân loại là không phải tiếng nói. Đặt `false` để tắt filter AI thử nghiệm này mà không đổi phần routing realtime còn lại. |
| `HAL_REALTIME_FIRST_CHUNK_MAX_CHARS` | `0` | Mặc định nói ngay câu hoàn chỉnh đầu tiên và tổng hợp trước các câu sau qua hàng đợi, không chờ toàn bộ câu trả lời. Giá trị dương bật cắt mệnh đề đầu, hoặc cắt theo khoảng trắng khi vượt giới hạn này; câu hoàn chỉnh bỏ qua bộ cắt. Giữ nguyên voice tag trong ngoặc vuông (kể cả khi cắt theo khoảng trắng), bỏ qua dấu phẩy/hai chấm trong số và dấu hai chấm của URL, yêu cầu 8 ký tự hiển thị ngoài tag. Cắt sớm vẫn có thể tạo khoảng ngắt giữa các yêu cầu tổng hợp. |
| `HAL_REALTIME_MIN_COMMIT_DURATION_S` | `0.8` | Session ngắn hơn ngưỡng này mà không có STT transcript bị coi là nhiễu VAD, không commit lên model. Chỉ xét khi `HAL_REALTIME_REQUIRE_TRANSCRIPT=false`. |
| `HAL_REALTIME_NOISE_GUARD_MAX_WORDS` | `3` | Mở rộng guard voiced-ratio của Silero sang cả turn CÓ transcript, tối đa ngần này từ. STT bịa một từ đệm ngắn từ tiếng ồn phòng và báo confidence tối đa cho nó, nên turn kiểu đó trước đây lọt hết mọi guard (guard chỉ chạy khi transcript rỗng) và commit nhiễu thuần lên model. Transcript nhiều nhất ngần này từ sẽ bị kiểm lại theo `HAL_REALTIME_NOISE_SPEECH_RATIO` và bị bỏ nếu audio chưa từng voiced; lệnh ngắn nói thật vẫn là voiced nên vẫn commit. Tỉ lệ được đo trên **span voiced** — từ chunk voiced đầu tới chunk voiced cuối — chứ không phải toàn buffer, vì bản capture luôn kèm pre-roll của VAD ở đầu và 200ms đuôi giữ lại ở cuối; phần đệm cố định đó làm loãng câu ngắn nặng hơn câu dài rất nhiều. Đo toàn buffer từng vứt nhầm một câu `Yes, that's right.` nói thật ở mức 0.500 (`peak=1.000`) — tức là guard quay ra phạt đúng lớp câu nó sinh ra để soi. Tiếng ồn kéo dài vẫn rớt, vì các chunk voiced của nó thưa ngay bên trong span. Transcript dài hơn không bao giờ bị kiểm lại, nên ngưỡng này không thể làm câm một câu nói thật. `0` = tắt. |
| `HAL_REALTIME_SESSION_IDLE_RESET_S` | `240` | Kiểm soát chi phí: khi một turn đến sau ngần này giây im lặng, recycle (rebuild) session **sau** turn đó để turn kế tiếp bỏ phần context mỗi-turn mà provider re-bill trên session sống lâu. Turn sau khoảng nghỉ dài coi như cuộc hội thoại mới; trí nhớ dài hạn vẫn còn nhờ nạp lại `summary.md`. Với Gemini native-audio, bước này bị bỏ qua nếu pre-turn recycle thành công đã làm mới session cho chính idle gap đó. `0` = tắt. Dùng lại đường rebuild của zombie-recovery. |
| `HAL_GEMINI_SESSION_RESUMPTION` | `false` | Resume cùng session Gemini qua reconnect. Mặc định OFF — proxy `campaign-api` không forward đúng resumption handshake nên resume qua nó tạo session zombie (cold reconnect thì chạy được). Chỉ bật khi endpoint hỗ trợ. |
| `HAL_GEMINI_IDLE_PARK_S` | `45` | Park Gemini khi idle: đóng transport của session sau ngần này giây không có hoạt động turn, để server không phải đóng nó bằng WS `1008` (backend ghi thành lỗi và bắn cảnh báo). Orchestrator vẫn `available` trong lúc parked; `prepare_turn()` của turn kế tiếp nối lại đồng bộ trước khi stream audio. Phải nhỏ hơn thời gian idle chết ngắn nhất đo được (86 giây). `0` = tắt. |
| `HAL_GEMINI_PRE_TURN_RECYCLE_S` | `60` | Guard transport cho Gemini: khi lượt nói mới bắt đầu sau ngần này giây idle, rebuild session Gemini **trước khi** stream pre-roll/audio để turn không đụng socket chết vì idle ở proxy/SDK. `0` = tắt. Pre-turn recycle thành công sẽ chặn idle recycle generic sau chính turn đó, nên một idle gap chỉ tạo tối đa một rebuild phục vụ transport/chi phí. |
| `HAL_AGENT_GATEWAY` | `openclaw` | Chọn context manager (cũng đọc từ `agent_runtime` trong config.json) |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | — | Key Gemini; fallback về `llm_api_key` |
| `HAL_GEMINI_LIVE_MODEL` | `gemini-2.5-flash-native-audio-preview-12-2025` | |
| `HAL_GEMINI_LIVE_VOICE` | `Kore` | |
| `HAL_GEMINI_LIVE_BASE_URL` | `<llm_base_url>/ws/gemini` | |
| `HAL_GEMINI_THINKING_LEVEL` | `MINIMAL` | `MINIMAL` \| `LOW` \| `MEDIUM` \| `HIGH` — default rẻ (trước là `HIGH`) |
| `HAL_GEMINI_GOOGLE_SEARCH` | `true` | Google Search grounding (chỉ Gemini). Cho model realtime tự trả lời câu dữ liệu công khai theo thời gian thực (thời tiết, tin tức, lookup) ngay trong phiên thay vì delegate. Tính phí theo mỗi grounded request (cộng token); chỉ phát sinh khi Gemini quyết định search. Cũng đặt được qua `realtime.gemini.google_search` trong config.json. |
| `HAL_GEMINI_VISION` | `true` | Tool `look` trong phiên (chỉ Gemini). Cho model realtime chụp một frame camera và trả lời câu hỏi thị giác ("cái này là gì?") ngay trong phiên thay vì delegate. Mặc định bật; chỉ đăng ký khi thiết bị còn có capability `vision`. Cũng đặt được qua `realtime.gemini.vision` trong config.json. |
| `HAL_GEMINI_VISION_MAX_WIDTH` | `768` | Bề rộng tối đa (px) frame được downscale trước khi gửi — giới hạn token ảnh. |
| `HAL_GEMINI_VISION_MIN_INTERVAL_S` | `10` | Chặn chi phí: số giây tối thiểu giữa hai lần **gửi ảnh**. Gọi `look` lặp trong khoảng này (hoặc gọi lần hai trong cùng turn) sẽ xài lại ảnh đã có trong context thay vì gửi ảnh mới. `0` = luôn gửi ảnh mới. |
| `HAL_GEMINI_VISION_HANDOFF_MAX_AGE_S` | `45` | Tuổi tối đa của frame `look` còn được bàn giao cho main agent khi delegate/timeout fallback để nó xài lại ảnh thay vì chụp lại. **Phải lớn hơn `HAL_REALTIME_LOOK_RECV_TIMEOUT_S` cộng thời gian dispatch** — nhánh timeout fallback chỉ chạy sau khi watchdog đó hết giờ, nên để bằng nhau là mọi frame đều hết hạn (cả hai cùng bằng `20` từ 2026-07-06 đến 2026-08-24 và handoff chưa từng bắn lần nào). `0` tắt guard tuổi (frame vẫn bị clear mỗi turn). |
| `OPENAI_API_KEY` | — | Key OpenAI; fallback về `llm_api_key` |
| `HAL_OPENAI_REALTIME_MODEL` | `gpt-realtime-2` | |
| `HAL_OPENAI_REALTIME_VOICE` | `alloy` | |
| `HAL_OPENAI_REALTIME_BASE_URL` | `<llm_base_url>/ws/openai` | |
| `HAL_OPENAI_REASONING_EFFORT` | `minimal` | `minimal` \| `low` \| `medium` \| `high` \| `xhigh` — default rẻ (trước là `xhigh`) |
| `DASHSCOPE_API_KEY` | — | Key Qwen (DashScope); **không** fallback về `llm_api_key` — chỉ đọc `realtime.qwen.api_key` khi env trống |
| `HAL_QWEN_REALTIME_BASE_URL` | — | WS host DashScope (`wss://<workspace-host>/api-ws/v1/realtime`); **không** fallback về `llm_base_url` — chỉ đọc `realtime.qwen.base_url` khi env trống |
| `HAL_QWEN_REALTIME_MODEL` | `qwen3.5-omni-plus-realtime` | turbo legacy: không gọi function call, lờ turn context |
| `HAL_QWEN_REALTIME_VOICE` | `Ethan` | 3.5-plus: thêm Serena; chỉ-turbo: Cherry \| Chelsie |
| `HAL_REALTIME_MEMORY_PATH` | `<workspace>/realtime/memory.jsonl` | |
| `HAL_REALTIME_MAX_MEMORY_ENTRIES` / `_TRIM_KEEP` | `1000` / `500` | |
| `HAL_REALTIME_SUMMARIZER_ENABLED` | `true` | |
| `HAL_REALTIME_SUMMARIZER_MODEL` | `claude-haiku-4-5-20251001` | Anthropic Messages API |
| `HAL_REALTIME_SUMMARIZER_RETRIES` | `2` | Số lần thử lại mỗi lượt summarize; `0` là tắt |
| `HAL_REALTIME_SUMMARIZER_RETRY_BACKOFF_S` | `1.5` | Chờ trước lần thử lại đầu, mỗi lần sau nhân đôi |

## Bản đồ code

| File | Vai trò |
|------|---------|
| `orchestrator.py` | Vòng đời session, tool `delegate_to_main` + `express_emotion` + `look`, stream lượt |
| `voice_agent/base.py` | Agent trừu tượng: contract 2-thread/queue, `receive()` |
| `voice_agent/gemini_live.py` | Provider Gemini Live (IO loop asyncio) |
| `voice_agent/openai_realtime.py` | Provider OpenAI Realtime (sync, connection serialize bằng lock) |
| `voice_agent/qwen_realtime.py` | Provider Qwen Omni Realtime (sync, `websockets.sync.client` thô, schema beta OpenAI qua DashScope; bảng giá `_QWEN_RATES` + log `qwen_usage.log`) |
| `context_manager/{base,openclaw,hermes}.py` | Lắp ráp prompt + memory + skills theo gateway |
| `summarizer.py` | Summarizer memory dựa trên Anthropic |
| `config.py` | Model config provider (`GeminiConfig`, `OpenAIConfig`) |
| `models/`, `enums/` | Kiểu input/output/event, enum provider + gateway |
| `resources/` | System prompt (chung + theo provider) |
| `../voice/voice_service.py` | Tích hợp: stream audio mic, tiêu thụ output, route delegate/handled. Chế độ live: `_live_decision` / `_live_session` / `_live_out_pump` / `_live_uplink_frame` |
| `../voice/aec.py` | WebRTC AEC3 trên đường mic; tham chiếu lấy tại TTS output stream (mọi provider) |

### Event hoàn tất agent của Buddy

Managed session trên desktop báo completed/needs_input/error qua sensing route chuẩn bằng type `buddy.agent.<session_id>`. Event thụ động này được queue khi agent hoặc speaker bận; type riêng theo session giữ được thông báo song song. Policy sleep và voice privacy hiện có vẫn áp dụng. Lamp dùng skill `agent-management` và project/session ID tường minh cho follow-up. Summary là dữ liệu kết quả không đáng tin cậy, không phải quyền chạy tool. Delivery là best effort; đọc `agent.session` là cách phục hồi trạng thái.

### Chuyển tiếp câu bổ sung cho tác vụ desktop trong realtime

Cả bốn biến thể prompt realtime và mô tả chung của `delegate_to_main` đều chuyển thao tác app desktop native cùng câu trả lời, sửa đổi hoặc yêu cầu dừng rõ ràng cho tác vụ main agent đang chờ về main agent; realtime không phát lời nói trong lượt chuyển tiếp. Message chỉ chứa lời người dùng vừa nói được hiểu rõ và tham số đã cung cấp, giữ đủ mọi vế yêu cầu. Ngữ cảnh tác vụ chỉ dùng nội bộ để quyết định chuyển tiếp; không thêm hoặc kể lại vì main agent đã giữ cuộc hội thoại. Câu ngắn như “cuối tuần này, hai người” có thể tiếp nối câu hỏi bổ sung cho việc tìm chỗ ở trước đó; không được bỏ chỉ vì thiếu động từ hành động hoặc tự suy diễn thành ngày cụ thể.

Câu hỏi main agent đã nói gần đây trong `[TTS HISTORY]` được dùng làm ngữ cảnh để hiểu câu tiếp nối, đồng thời vẫn giữ quy tắc không nói lặp. `[TTS HISTORY, not spoken]` không chứng minh người dùng đã nghe hoặc trả lời câu hỏi đó. Kiểm tra hội thoại nền và việc lời nói có hướng tới device vẫn giữ nguyên. Thay đổi này dùng lịch sử bàn giao/câu trả lời realtime sẵn có; không thêm kho trạng thái tác vụ đang chờ có cấu trúc, không tự chứng minh voice routing trên thiết bị thật đã thành công và không loại bỏ giới hạn truyền context theo provider.

Message chuyển tiếp phải giữ tên ứng dụng và nội dung đọc để ghi, không chỉ chủ đề chung. Ví dụ “Ghi vào Notes là chiều mua sữa” phải giữ Notes và nguyên văn “chiều mua sữa”; rút thành lời nhắc mua sữa chung làm mất cả đích lẫn nội dung. Các biến thể prompt và mô tả tool nay nêu rõ yêu cầu này. Một quan sát bằng audio tổng hợp đã cho thấy message chuyển tiếp bị mất thông tin; khi thiếu transcript đầu vào, chưa thể tách lỗi nhận dạng âm thanh khỏi lỗi tóm tắt, và thay đổi câu chữ vẫn cần kiểm chứng hành vi.

Yêu cầu hoặc câu bổ sung hiện tại được chuyển tiếp bằng ngôn ngữ người dùng vừa nói, không thêm bình luận hoặc tóm tắt các lượt trước. Dịch sang tiếng Anh có thể khiến main agent trả lời sai ngôn ngữ vì instruction chuyển tiếp là đầu vào chính của nó.

Một lượt so sánh audio tổng hợp riêng bằng Gemini 3.1 Live sau đó dùng cùng PCM cho prompt/tool baseline và bản cuối. Message chuyển tiếp của bản cuối là “Ghi vào Notes là sáng mai tưới cây.”, “Mở Airbnb tìm chỗ ở Đà Nẵng giúp mình.” và câu tiếp nối “cuối tuần này hai người”. Baseline đã đổi yêu cầu Notes thành “Remember to water the plants tomorrow morning.”, làm mất tên app và đổi ngôn ngữ. Câu tiếp nối Airbnb chạy trong cùng phiên provider sau câu hỏi bổ sung `[TTS HISTORY]` có kiểm soát; đã xác nhận ranh giới hoàn tất lượt trước từ server và commit audio mới. Kết quả này chứng minh hành vi chuyển tiếp quan sát được cho các clip tổng hợp đó, không chứng minh microphone/wake-word, câu hỏi thật từ main agent hoặc hoàn thành toàn luồng main-agent/desktop. Kết quả cuối riêng được lưu tại `/tmp/buddy-rt-final/result.json` trên thiết bị kiểm thử; lượt đánh giá không thay prompt production hoặc dịch vụ đang chạy.
