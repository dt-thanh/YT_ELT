# YT\_ELT Studio — Đặc tả product AI Faceless Studio

Oct 30, 2026 · @William DG

## 0. Tóm tắt và quyết định đã chốt

YT\_ELT chuyển từ "phân tích kênh của chính mình" thành **YT\_ELT Studio**: một studio nội dung không lộ mặt do 7 agent vận hành, với Thành là người duyệt cuối. Tài liệu này thay thế "YT\_ELT — Đặc tả nâng cấp AI Engineering.docx" (15/10/2026). Phần đã làm trong đặc tả cũ vẫn giữ và trở thành bộ phận Nghiên cứu.

Nguyên tắc một câu: **học xu hướng từ người khác, lấy nội dung từ nguyên tác, viết theo giọng của mình.**

| # | Quyết định | Giá trị chốt |
| --- | --- | --- |
| D1 | Mô hình | Một kênh YouTube tiếng Việt, không lộ mặt, có Shorts và video dài |
| D2 | Chuyên mục | Cả 3: Khắc kỷ ứng dụng, Triết lý phương Đông, Ngụ ngôn và truyện cổ |
| D3 | Nguồn theo dõi | Kênh tiếng Anh, tiếng Trung và tiếng Việt; ưu tiên chủ đề đã lên ở EN/ZH nhưng chưa có ở VI |
| D4 | Tín hiệu | Chế độ cơ bản: view, like, số bình luận, mức tăng view của từng video. Tín hiệu nâng cao, gom cụm chủ đề và phân tích bình luận chỉ bật khi derived\_metrics\_enabled = true sau khi được YouTube chấp thuận (R2) |
| D5 | Nội dung | Chỉ viết từ nguyên tác thuộc phạm vi công cộng; không nhại video của kênh khác |
| D6 | Mức tự động | 3 điểm duyệt của người: chủ đề, kịch bản, video cuối; người bấm đăng |
| D7 | Mảng trẻ em | Hoãn sang sau khi kênh chính đạt chuẩn chất lượng |
| D8 | Thứ tự | Đóng giai đoạn 0 của YT\_ELT trước hạn VietinBank 06/11/2026; studio bắt đầu sau đó |

Agent nào đọc tài liệu này để triển khai: làm theo thứ tự backlog ở mục 14, mỗi mục có ID và tiêu chí nghiệm thu. Không bỏ qua điểm duyệt của người.

## 1. Bối cảnh: YT\_ELT đã có gì và vì sao đổi hướng

YT\_ELT (commit 3defe3b) đã là một hệ thu thập và phân tích dữ liệu YouTube chạy được, với 301 unit test pass. Đây chính là bộ phận Nghiên cứu của studio, nên gần như toàn bộ được giữ lại.

| Thành phần đã có | Vị trí | Vai trò trong studio |
| --- | --- | --- |
| Thu thập kênh, video, thống kê, bình luận | src/youtube\_intel/youtube.py, pipeline.py, dags/yt\_collect.py | Giữ, mở rộng lên 60–90 kênh đa ngôn ngữ |
| Lưu thô trên MinIO, chuẩn hóa vào Postgres | storage.py, normalize.py, sql/migrations 001–007 | Giữ |
| Kiểm tra chất lượng dữ liệu | quality.py, Soda, dags/yt\_quality.py | Giữ |
| Tốc độ view theo giờ | view v\_video\_growth (views\_per\_hour, validity\_reason) | Nền cho tín hiệu tốc độ |
| RAG trên tiêu đề, mô tả, bình luận | rag/ (pgvector, RRF k=60) | Nền cho gom chủ đề và phân tích bình luận |
| Trợ lý hỏi đáp có tool chỉ đọc | assistant/ (8 tool + submit\_answer, grounding) | Trở thành agent Nghiên cứu |
| Bản tin | reporting.py, dags/yt\_report.py | Trở thành agent Báo cáo |
| Eval | evals.py, evals/assistant\_cases.yaml (45 ca), rag\_queries.yaml | Mở rộng cho từng agent |
| Xóa dữ liệu cũ | retention.py | Bắt buộc theo chính sách API |
| Dashboard | dashboard/app.py (6 màn hình) | Thêm hộp thư duyệt |

**Việc còn dở của đặc tả cũ** (đưa vào giai đoạn P0):

- Grounding chấp nhận số gắn sai thực thể: đổi số của video A và B, hoặc sai thứ hạng, vẫn qua (G-01).
- Grounding sai với chiều giảm: delta -1200 với câu "giảm 1.200" bị coi là không có căn cứ (G-02).
- 25 câu RAG eval chưa có nhãn relevant.
- Cờ derived\_metrics\_enabled chưa nối vào code.
- rag.min\_similarity = 0.25 là giá trị đoán, chưa hiệu chỉnh.
- Chưa chạy live đủ và chưa lưu kết quả eval của trợ lý.
- docs/policy\_notes.md chưa xác minh.

**Vì sao đổi hướng:** mục đích thật là làm nội dung, không phải bán công cụ phân tích. Một studio chạy thật cũng là bài toán AI engineering đầy đủ hơn: multi-agent, RAG có trích dẫn, pipeline media, đánh giá nội dung sáng tạo, chi phí và vòng phản hồi từ số liệu thật.

## 2. Mục tiêu, phi mục tiêu, chỉ số thành công

Mục tiêu 6 tháng đầu là một studio chạy ổn định và đo được, chưa phải doanh thu.

**Mục tiêu**

1. Biết mỗi tuần nên làm chủ đề gì, có bằng chứng số liệu từ nhiều kênh và nhiều ngôn ngữ.
2. Sản xuất đều 3 video mỗi tuần (Shorts hoặc video dài) với thời gian công sức của người dưới 1 giờ mỗi video.
3. Mọi câu trích và mọi con số trong kịch bản, bản tin đều có nguồn kiểm chứng được.
4. Không vi phạm chính sách YouTube: không cảnh cáo, không bị từ chối kiếm tiền vì nội dung rập khuôn.
5. Hệ thống tự học: sau mỗi video, biết tín hiệu nào dự đoán đúng.

**Phi mục tiêu (không làm)**

- Không đăng tự động mà không qua người duyệt.
- Không sản xuất hàng loạt để đánh số lượng.
- Không công bố bảng xếp hạng hay "score" của kênh người khác.
- Không tải hoặc cào phụ đề, video của kênh khác.
- Không làm kênh trẻ em trong giai đoạn này.
- Không bán công cụ cho người khác cho tới khi studio của chính mình chạy tốt.

**Chỉ số thành công**

| Chỉ số | Mục tiêu | Đo bằng |
| --- | --- | --- |
| Video đăng mỗi tuần | 3 | Bảng publications |
| Thời gian người làm mỗi video | dưới 60 phút | Tổng human\_effort\_minutes của job (không tính thời gian chờ giữa các điểm duyệt) |
| Chi phí API mỗi video | dưới trần cấu hình (mặc định 3 USD cho Short, 10 USD cho video dài) | Bảng llm\_calls và media\_jobs |
| Tỉ lệ câu trích khớp nguyên tác | 100% | Agent Kiểm định |
| Tỉ lệ bị người bác ở mỗi điểm duyệt | giảm dần theo tháng | Bảng approvals |
| Giữ chân người xem (retention) | tăng dần theo tháng; mốc cụ thể đặt sau 10 video đầu | YouTube Analytics API |
| Ngưỡng YPP | theo dõi, không ép | YouTube Studio |

## 3. Luật chơi bắt buộc

Tám luật dưới đây là ràng buộc cứng của hệ thống. Mỗi luật có một kiểm tra tự động tương ứng ở agent Kiểm định hoặc trong code. Đây là tóm tắt kỹ thuật, không phải tư vấn pháp lý.

| ID | Luật | Hệ quả nếu vi phạm | Kiểm soát trong hệ thống |
| --- | --- | --- | --- |
| R1 | YouTube (cập nhật 07/2026) không cho kiếm tiền với nội dung chung chung, lặp lại, theo mẫu, dễ sản xuất hàng loạt; nội dung gây khó chịu để câu view; persona AI về chủ đề nhạy cảm ([TechCrunch](https://techcrunch.com/2026/07/20/youtube-clarifies-policies-around-ai-slop-and-upsetting-videos/)) | Cả kênh có thể mất kiếm tiền | Format riêng 3 phần; kiểm tra trùng lặp bằng embedding so với video cũ; giới hạn số video/tuần; người duyệt kịch bản |
| R2 | Derived metrics: nếu chưa được chấp thuận, không được dùng API Data để tạo metric hay dữ liệu mới, cũng không được thay số liệu API bằng số tự tính (Developer Policies III.E.4.h). Điểm số và tỉ lệ tự tính, gắn tag hay phân loại nội dung, phân tích cảm xúc bình luận, xếp hạng giữa các kênh chỉ được phép khi dự án chấp nhận phần sửa đổi chính sách với use case Analytics & Reporting (III.L) ([Developer Policies](https://developers.google.com/youtube/terms/developer-policies), [Derived Metrics Policy](https://developers.google.com/youtube/terms/derived-metrics-policy)) | Giảm quota hoặc thu hồi quyền API | Cờ derived\_metrics\_enabled (mặc định false) chặn mọi tính năng nâng cao liệt kê ở mục 8. Khi cờ tắt chỉ còn số thô, sắp xếp, lọc và mức tăng view của từng video, luôn hiển thị cạnh số thô và ghi rõ "tính độc lập". Đây là cách phân ranh thận trọng, không phải tư vấn pháp lý; trường hợp chưa rõ thì xếp vào nâng cao |
| R3 | Lưu trữ (III.E.4): Non-Authorized Data (dữ liệu công khai lấy bằng API key, gồm dữ liệu mọi kênh đang theo dõi) chỉ được lưu tạm và tối đa 30 ngày; thống kê Non-Authorized cũng không quá 30 ngày. Authorized Data (kênh của mình, qua OAuth): thống kê và Analytics lưu theo nhu cầu nhưng phải kiểm lại quyền mỗi 30 ngày; dữ liệu Authorized khác thì xóa hoặc làm mới sau 30 ngày. Người dùng yêu cầu xóa thì xóa trong 7 ngày | Vi phạm điều khoản | Bảng lưu trữ theo lớp dữ liệu ngay dưới; retention.py xóa theo fetched\_at và data\_class |
| R4 | Không tải phụ đề hay video của kênh khác (captions.download cần quyền chủ kênh) | Vi phạm điều khoản | Agent chỉ có metadata, thống kê và bình luận công khai |
| R5 | Bản quyền: chỉ dùng văn bản thuộc phạm vi công cộng hoặc bản dịch đã hết hạn; không dùng bìa sách, nhân vật có chủ (Peppa, Wolfoo...); truyện cổ tích kể lại bằng lời mình, không chép tuyển tập | Khiếu nại bản quyền, gỡ video | Mỗi đoạn trong kho kinh điển mang trường license; validator reference\_names chặn tên có chủ |
| R6 | Khai báo nội dung AI: nội dung AI trông như thật phải khai báo; TTS cho nội dung gốc không bắt buộc; voice clone người thật thì phải ([MiniMatters](https://minimatters.com/?p=6160)) | YouTube tự gắn nhãn, có thể phạt | Agent Kiểm định đặt cờ cần khai báo; khi upload qua API dùng trường status.containsSyntheticMedia; mô tả video luôn ghi có dùng AI |
| R7 | Nội dung dành cho trẻ em: phải tự khai báo made for kids; video AI trên YouTube Kids đang bị giới hạn ở một nhóm nhỏ kênh đã thẩm định ([YourStory](https://yourstory.com/ai-story/youtube-ai-slop-kids-ban)) | Phạt, mất kiếm tiền | Chuyên mục ngụ ngôn kể cho người lớn, phong cách hình ảnh người lớn; status.selfDeclaredMadeForKids = false có chủ đích, ghi lý do |
| R8 | Bình luận là dữ liệu của người khác | Rủi ro quyền riêng tư | Không lưu tên hiển thị; mã hóa một chiều ID tác giả; không trích nguyên văn bình luận vào video |

**Quy tắc lưu trữ theo lớp dữ liệu (R3).** Mọi bảng và file chứa API Data có cột fetched\_at và data\_class. Mặc định là không lưu dài hạn thống kê công khai khi chưa có quyền.

| Lớp dữ liệu | Ví dụ | Cờ tắt (mặc định) | Cờ bật, đã được chấp thuận |
| --- | --- | --- | --- |
| Snapshot thô | JSON API lưu trên MinIO | Tối đa 30 ngày | Tối đa 30 ngày (chứa cả tiêu đề, mô tả); phần thống kê được tách sang bảng thống kê |
| Thống kê Non-Authorized | view, like, số bình luận, số subscriber của kênh đang theo dõi | Tối đa 30 ngày, sau đó xóa hoặc làm mới | Tối đa 36 tháng |
| Metadata và văn bản | tiêu đề, mô tả, tên kênh, văn bản bình luận | Tối đa 30 ngày, sau đó xóa hoặc làm mới | Vẫn tối đa 30 ngày |
| Dữ liệu suy ra | S1–S6, cụm chủ đề, nhãn và cảm xúc bình luận, embedding | Không tạo; embedding phục vụ tìm kiếm hiện có bị xóa cùng văn bản gốc | Kết quả dạng số hoặc nhãn: tối đa 36 tháng. Embedding, tóm tắt, câu diễn đạt lại từ văn bản gốc: theo hạn của văn bản gốc (30 ngày) |
| Dữ liệu kênh của mình (Authorized) | Analytics, retention, thống kê video của mình | Lưu theo nhu cầu; kiểm lại quyền OAuth mỗi 30 ngày; dữ liệu Authorized khác 30 ngày | Như cột bên trái |
| Dữ liệu không đến từ API | canon, kịch bản, media, approvals, chi phí, publications | Không giới hạn | Không giới hạn |

Hệ quả cho thiết kế: khi cờ tắt, mọi so sánh theo thời gian chỉ dùng dữ liệu trong 30 ngày gần nhất. Nguồn: [Developer Policies](https://developers.google.com/youtube/terms/developer-policies) (III.E.4), [Derived Metrics Policy](https://developers.google.com/youtube/terms/derived-metrics-policy) (mục lưu trữ).

**Upload qua API:** videos.insert tốn 1 đơn vị trong nhóm quota "Video Uploads", giới hạn 100 lần gọi/ngày. Trang tài liệu hiện ghi video từ dự án chưa xác minh không bị giới hạn ở chế độ riêng tư, và privacyStatus mặc định là public ([videos.insert](https://Developers.Google.com/youtube/v3/docs/videos/insert)). Vì vậy code luôn gửi privacyStatus = private, rồi người chuyển sang public trong Studio.

## 4. Mô hình kinh doanh và số liệu tham chiếu

Ngách triết lý và bài học từ sách có RPM ước tính cao hơn ngách trẻ em khoảng 2 lần. Các số dưới đây là ước tính của trang công cụ, dùng để lập kế hoạch, không phải số kiểm toán. Chưa có số liệu đáng tin cho RPM nội dung tiếng Việt; số này phải đo trên kênh thật.

| Ngách | CPM ước tính (USD) | Ghi chú |
| --- | --- | --- |
| Động lực, tóm tắt sách | 6–15 | RPM creator nhận khoảng 3,3–8 USD; 1 triệu view/tháng cho khoảng 3.300–8.000 USD từ quảng cáo |
| Lịch sử | 5–12 | Chủ đề phụ về sau |
| Trẻ em, giáo dục | 3–8 | Video made for kids bị tắt quảng cáo cá nhân hóa và bình luận |

Nguồn: [OutlierKit](https://alpha.outlierkit.com/resources/faceless-youtube-channels/).

**Ví dụ tham chiếu (ước tính):** Pursuit of Wonder (triết học, 3,5 triệu sub) khoảng 4.000–13.000 USD/tháng. Các kênh trẻ em làm bằng AI lớn nhất ước hơn 4,25 triệu USD/năm, nhưng đang bị siết mạnh.

**Mặt trái:** video từ kênh "faceless automation" vượt 10 lần mức trung bình của chính kênh chỉ trong 3,1% trường hợp, so với 16,7% ở ngách tài chính cá nhân. Làm hàng loạt hiếm khi bùng nổ; chất lượng và góc nhìn riêng mới là biến quyết định.

**Nguồn thu theo thứ tự khả thi**

1. Affiliate sách: đặt link tới nơi bán bản dịch hoặc sách liên quan. Làm được ngay, không cần YPP.
2. Quảng cáo YouTube: sau khi đạt điều kiện YPP. Kiểm tra ngưỡng hiện hành trên trang YouTube.
3. Phiên bản tiếng Anh của cùng nội dung: RPM cao hơn, tái dùng kịch bản và hình ảnh.
4. Về sau: bán chính công cụ studio cho creator khác, khi đã có bằng chứng từ kênh của mình. Đối thủ tham chiếu: 1of10 (29–69 USD/tháng), OutlierKit (24,92–166,58 USD/tháng).

**Chi phí tạo video tham chiếu** ([DevTk, 25/10/2026](https://devtk.ai/en/blog/ai-video-generation-pricing-2026/)):

| Model | Giá (USD/giây) | Ghi chú |
| --- | --- | --- |
| Grok Imagine 1.5 Lite | 0,02 (480p) – 0,03 (720p) | Rẻ nhất |
| MiniMax Hailuo 2.3 | khoảng 0,047–0,082 | Clip 6 hoặc 10 giây |
| Veo 3.1 Lite | 0,05 (720p) – 0,08 (1080p) | Có audio |
| Kling 3.0 | 0,084–0,42 |  |
| Veo 3.1 Fast | 0,10–0,30 |  |
| Veo 3.1 | 0,40–0,60 | Chất lượng cao nhất |

API Sora 2 đã đóng từ 24/09/2026. Chiến lược chi phí: phần thân dùng ảnh tĩnh có chuyển động nhẹ cộng TTS; chỉ dùng clip AI cho 3–5 giây mở đầu. Ví dụ: 5 giây Veo 3.1 Lite 720p là 0,25 USD. Giá tạo ảnh và TTS điền khi chọn nhà cung cấp (câu hỏi mở Q2).

## 5. Kênh, chuyên mục, format và nguồn theo dõi

Một kênh tiếng Việt với 3 chuyên mục chạy song song. Mỗi chuyên mục có nguồn nguyên tác và phong cách hình ảnh riêng, nhưng cùng một giọng đọc và một format.

| Mã | Chuyên mục | Nguyên tác chính | Phong cách hình ảnh | Ví dụ tiêu đề |
| --- | --- | --- | --- | --- |
| stoic | Khắc kỷ ứng dụng | Marcus Aurelius (Suy tưởng), Seneca (Thư gửi Lucilius, Về sự ngắn ngủi của đời người, Về cơn giận), Epictetus (Cẩm nang) | Tượng đá, tranh sơn dầu cổ điển, tông trầm | Marcus Aurelius sẽ làm gì khi bị sếp mắng oan? |
| east | Triết lý phương Đông | Lão Tử (Đạo Đức Kinh), Trang Tử, Khổng Tử (Luận Ngữ), Tôn Tử (Binh pháp) | Tranh thủy mặc, nhiều khoảng trống | Bào Đinh mổ bò: bí quyết làm việc 19 năm không mệt |
| fable | Ngụ ngôn và truyện cổ (kể cho người lớn) | Aesop, La Fontaine, truyện cổ tích dân gian Việt Nam | Minh họa phẳng tông ấm, nhân vật tự thiết kế, không kiểu hoạt hình trẻ em | Rùa và Thỏ: thật ra con thỏ không thua vì lười |

**Format cố định (chống nội dung rập khuôn)**

1. **Nguyên tác:** câu trích hoặc tình tiết gốc, ghi rõ tác phẩm, quyển, chương.
2. **Tình huống người Việt hôm nay:** công sở, gia đình, tiền bạc, mạng xã hội. Phần này là góc nhìn riêng của kênh.
3. **Một việc làm được ngay:** một bài tập cụ thể cho người xem.

| Định dạng | Độ dài | Cấu trúc |
| --- | --- | --- |
| Short 9:16 | 30–60 giây | 3 giây mở đầu bằng câu hỏi hoặc câu trích, 1 tình huống, 1 bài tập |
| Video dài 16:9 | 6–10 phút | Mở đầu, bối cảnh tác giả, 2–3 đoạn nguyên tác, mỗi đoạn một tình huống, tổng kết bài tập |

Short có thể cắt từ video dài, nhưng phải có mở đầu riêng.

**Nguồn theo dõi**

| Ngôn ngữ | Số kênh mục tiêu | Vai trò | Ghi chú |
| --- | --- | --- | --- |
| Tiếng Anh | 25–35 | Nguồn xu hướng chính, đi trước thị trường Việt | Stoic, philosophy, book lessons, fables |
| Tiếng Trung | 15–25 | Nguồn cho chuyên mục phương Đông và ngụ ngôn; văn hóa gần Việt Nam | Chỉ kênh trên YouTube (Đài Loan, Hồng Kông, Malaysia, Hán ngữ hải ngoại). Bilibili và Douyin nằm ngoài phạm vi vì không dùng chung YouTube API và có điều khoản riêng |
| Tiếng Việt | 15–25 | Đo độ bão hòa: chủ đề nào đã có người làm | Cả kênh triết lý, sách, kỹ năng sống |

Tiêu chí chọn kênh: còn hoạt động trong 60 ngày, đúng một trong 3 chuyên mục, có cả kênh lớn lẫn kênh nhỏ (kênh nhỏ cho tín hiệu "vượt mức thường" rõ hơn). Danh sách kênh do người chốt; agent chỉ đề xuất ứng viên. Lưu ý quota: search.list có bucket quota riêng, mặc định 100 lần gọi/ngày, mỗi lần 1 đơn vị (mục 8.4). Chỉ dùng khi tìm kênh mới, không dùng trong thu thập hằng ngày.

config/channels.yaml hiện có 4 kênh. Giữ FightMediocrity và Sprouts, chuyển Wolfoo Tiếng Việt và Heo Peppa Tiếng Việt sang active: false (chỉ giữ trong reference\_names để chặn IP).

## 6. Kiến trúc tổng thể

YT\_ELT hiện tại trở thành lớp dữ liệu dùng chung; các agent mới nằm trên nó và do Producer điều phối.

&#91;embedded content: kiến trúc YT\_ELT Studio · 7 agent, 3 điểm duyệt\]

Luồng chính đi từ trái sang phải: Research ra brief, Writer viết, Checker kiểm, Production dựng. Checker fail thì quay về Writer tối đa 2 lần, sau đó chuyển cho bạn. Analyst đọc số liệu sau khi đăng và ghi learnings để lần sau Research và Writer làm tốt hơn. Không agent nào có quyền đăng lên YouTube.

## 7. Đặc tả 7 agent

Mỗi agent là một node trong graph LangGraph, có input và output là JSON theo schema Pydantic, chỉ được dùng tool trong danh sách của mình. Không agent nào có quyền ghi vào YouTube.

| Agent | Model | Tool được dùng | Output |
| --- | --- | --- | --- |
| Producer (điều phối) | tầng rẻ | create\_job, get\_job\_state, request\_approval | Job plan |
| Research (nghiên cứu) | tầng rẻ | 8 tool chỉ đọc của YT\_ELT + get\_signals, get\_topic\_clusters, get\_comment\_insights | TopicBrief |
| Report (báo cáo) | tầng rẻ | giống Research + get\_own\_channel\_analytics | Bản tin Markdown |
| Writer (biên kịch) | tầng tốt | search\_canon, get\_passage, get\_style\_guide, get\_past\_scripts | Script + ScenePlan |
| Production (sản xuất) | không dùng LLM, trừ khi sửa prompt ảnh | tts\_synthesize, image\_generate, video\_generate, ffmpeg\_render | MediaBundle |
| Checker (kiểm định) | tầng tốt, khác model với Writer nếu được | get\_passage, similarity\_check, policy\_rules, media\_probe | CheckReport |
| Analyst (phân tích sau đăng) | tầng rẻ | get\_own\_channel\_analytics, get\_signals\_at\_time, write\_learning | Learning |

### 7.1 Producer

- **Nhận:** yêu cầu tự do của người ("tuần này 3 Shorts khắc kỷ", "làm video dài về Lão Tử từ brief số 12") hoặc lịch hằng tuần.
- **Làm:** tạo job, gọi các agent theo thứ tự, dừng ở 3 điểm duyệt (interrupt), tiếp tục khi người duyệt, quay lại bước trước khi bị bác kèm lý do.
- **Ràng buộc:** tối đa 2 lần sửa tự động giữa Writer và Checker rồi chuyển cho người; dừng job khi chi phí vượt trần; không tự tạo quá số video/tuần cấu hình.
- **Trạng thái job:** planned → researching → awaiting\_topic\_approval → writing → checking → awaiting\_script\_approval → producing → qc → awaiting\_final\_approval → ready\_to\_publish → published (khi publications đã có youtube\_video\_id, nhập tay hoặc từ P7) → analyzed. Nhánh rejected và failed từ mọi trạng thái.

### 7.2 Research

- **Nhận:** chuyên mục, cửa sổ thời gian (mặc định 14 ngày), số brief cần.
- **Làm:** đọc tín hiệu đã tính sẵn (mục 8), chọn cụm chủ đề, đọc bình luận, tìm nguyên tác phù hợp trong kho kinh điển, viết TopicBrief.
- **TopicBrief gồm:** topic\_id; tiêu đề làm việc; chuyên mục; bằng chứng (danh sách video\_id kèm số liệu thô và ngày chụp); ngôn ngữ đã lên (en, zh, vi); mức bão hòa tiếng Việt; các góc đã bị khai thác; góc đề xuất; câu hỏi của người xem từ bình luận; passage\_id nguyên tác gợi ý; độ tin cậy.
- **Ràng buộc:** mọi con số đi qua grounding đã sửa G-01, G-02; không số nào không có trong kết quả tool; khi derived\_metrics\_enabled = false, brief chỉ dùng số thô, mức tăng view của từng video và link video để người tự đọc; các trường cần tín hiệu nâng cao (ngôn ngữ đã lên, mức bão hòa, câu hỏi từ bình luận) để trống.

### 7.3 Report

- **Bản tin thị trường hằng ngày:** chủ đề mới nổi, video vượt mức thường, khoảng trống EN/ZH so với VI, câu hỏi nổi bật trong bình luận (các mục này cần cờ bật). Khi cờ tắt, bản tin chỉ gồm video mới và mức tăng view theo số thô của từng kênh.
- **Bản tin kênh mình hằng tuần:** view, retention, nguồn traffic, so sánh video tuần này với tuần trước, chi phí tuần.
- **Ràng buộc:** dùng lại reporting.py và kiểm tra grounding; bản tin chỉ cho người vận hành, không công bố.

### 7.4 Writer

- **Nhận:** TopicBrief đã duyệt, các passage nguyên tác, style guide của chuyên mục, 5 kịch bản gần nhất của kênh (để tránh lặp).
- **Script gồm:** tiêu đề (3 phương án), mô tả, danh sách đoạn; mỗi đoạn có loại (hook, nguyên tác, tình huống, bài tập, kết), lời đọc, và citation (passage\_id + đoạn trích) nếu là nguyên tác.
- **ScenePlan gồm:** mỗi cảnh có thời lượng ước tính, prompt ảnh hoặc video theo style guide, chuyển động (zoom, pan), chữ trên màn hình.
- **Ràng buộc:** câu trích phải lấy từ get\_passage, không tự viết lại rồi gắn tên tác giả; phần dịch sang tiếng Việt ghi rõ "dịch bởi kênh"; không nhắc tên kênh đối thủ.

### 7.5 Production

- Chạy pipeline mục 10 theo ScenePlan. Phần lớn là code xác định, không phải LLM.
- Ghi mỗi lần gọi API vào media\_jobs (nhà cung cấp, model, số giây, chi phí, đường dẫn MinIO).
- Thử lại tối đa 2 lần mỗi cảnh; hết lượt thì dùng ảnh tĩnh dự phòng.

### 7.6 Checker

Maker-checker: Checker không thấy lập luận của Writer, chỉ thấy kết quả. Mỗi kiểm tra trả pass, warn hoặc fail kèm lý do.

| Nhóm | Kiểm tra | Loại |
| --- | --- | --- |
| Nguồn | Citation chính xác: đoạn trích bằng ngôn ngữ gốc khớp passage nguồn sau chuẩn hóa (không dùng LLM). Fail thì chặn job | Code |
| Nguồn | Lời đọc không gán cho tác giả điều nguyên tác không nói | LLM-judge |
| Bản dịch | Bản dịch tiếng Việt giữ đúng nghĩa đoạn gốc: dịch ngược + LLM-judge, ra điểm và nhận xét, không so khớp chính xác. Kết quả ghi riêng với kiểm tra citation; dưới ngưỡng thì gắn cảnh báo cho người duyệt. Người vẫn duyệt bản dịch ở bước kịch bản | LLM-judge |
| Chính sách | Đủ 3 phần format; độ giống với mọi video cũ dưới ngưỡng | Code + embedding |
| Chính sách | Không có reference\_names; không tên kênh đối thủ | Code |
| Chính sách | Không gây sốc, không lời khuyên y tế, tài chính, không giảng đạo | LLM-judge |
| Chính sách | Cờ containsSyntheticMedia và made for kids, có lý do | Luật + LLM-judge |
| Kỹ thuật | Độ dài đúng định dạng; âm lượng chuẩn hóa; phụ đề khớp lời đọc; độ phân giải đúng | ffprobe |

### 7.7 Analyst

- Chạy sau khi video đăng 7 ngày và 28 ngày. Video đăng thủ công được nhận biết qua youtube\_video\_id mà người nhập vào publications, nên Analyst chạy được trước khi có P7.
- So retention, view, bình luận của video với tín hiệu lúc chọn chủ đề.
- Ghi Learning vào bảng learnings: nội dung, bằng chứng (video\_id, số liệu), độ tin cậy, ngày hết hạn. Research và Writer đọc learnings còn hiệu lực.
- Ràng buộc: một video không đủ để kết luận; learning cần tối thiểu 3 video cùng chiều mới được độ tin cậy cao.

## 8. Thuật toán tín hiệu và phân tích bình luận

Tín hiệu được tính bằng SQL và Python xác định trong DAG, không do LLM tính; LLM chỉ đọc kết quả. Mọi số tự tính luôn lưu và hiển thị kèm số thô của API, chỉ dùng nội bộ. Ngưỡng ban đầu là giá trị đoán, sẽ hiệu chỉnh bằng dữ liệu của agent Analyst.

Cờ derived\_metrics\_enabled (R2) chia tính năng thành hai chế độ. Cờ mặc định là false, chỉ được bật khi dự án được YouTube chấp thuận use case Analytics & Reporting.

| Tính năng | Chế độ | Ghi chú |
| --- | --- | --- |
| Số thô: view, like, số bình luận, thời lượng | Cơ bản | Lấy thẳng từ API |
| Sắp xếp, lọc theo số thô | Cơ bản |  |
| Mức tăng view và views\_per\_hour của một video giữa hai snapshot trong cửa sổ lưu trữ | Cơ bản | Hiển thị cạnh số thô, ghi "tính độc lập" |
| S1, S2 (so với mức thường của kênh) | Nâng cao |  |
| S3 (tỉ lệ like/view, comment/view) | Nâng cao | Tỉ lệ tự tính |
| S4–S6, gom cụm và phân loại chủ đề | Nâng cao |  |
| Phân loại và cảm xúc bình luận (8.3) | Nâng cao |  |
| So sánh hoặc xếp hạng giữa các kênh | Nâng cao | Không bao giờ công bố |
| Lưu thống kê công khai quá 30 ngày | Nâng cao | Tối đa 36 tháng (R3) |

### 8.1 Tín hiệu cấp video

**S1 — Vượt mức thường của kênh (nâng cao).** So view của video ở tuổi a ngày với trung vị view ở cùng tuổi của các video cùng kênh **và cùng định dạng** (Short hoặc video dài). Không so Short với video dài vì mức view của hai loại khác hẳn nhau.

```latex
\mathrm{S1}(v) = \frac{\mathrm{views}_v(a)}{\operatorname{median}_{u \in \mathrm{peers}(c,\,f)} \mathrm{views}_u(a)}, \quad a \in \{2, 7, 28\}\ \text{ngày}
```

peers(c, f) là tối đa 20 video gần nhất của kênh c có cùng định dạng f và có snapshot ở tuổi a. Định dạng lấy theo thời lượng trong contentDetails.duration: đến 180 giây coi là Short. Đây là heuristic, lưu kèm cờ format\_guess.

Baseline đầy đủ cần ít nhất 5 video cùng định dạng có snapshot ở tuổi a. Nếu thiếu, dùng trung vị view hiện tại của các video cùng định dạng 7–60 ngày tuổi, gắn baseline\_approx = true và hiển thị cảnh báo. Video có baseline\_approx chỉ được gắn "cần xem", không bao giờ tự động gắn "nổi trội" và không được tính vào S4. Ngưỡng ban đầu khi baseline đầy đủ: S1 từ 2 trở lên là "nổi trội", từ 5 trở lên là "rất nổi trội".

**S2 — Tốc độ.** views\_per\_hour trong 24–72 giờ đầu, lấy từ v\_video\_growth (chỉ dùng dòng validity\_reason = ok), chia cho trung vị tốc độ cùng giai đoạn của kênh. Phát hiện sớm hơn S1.

**S3 — Tương tác.** like/view và comment/view. comment/view cao nghiễu là chủ đề gây tranh luận hoặc khiến người xem muốn hỏi thêm: đây là tín hiệu ưu tiên để đọc bình luận.

### 8.2 Tín hiệu cấp chủ đề

**S4 — Chủ đề đang lên.** Nhúng tiêu đề và đầu mô tả bằng model đa ngôn ngữ (bge-m3, đã dùng ở RegScan) để video tiếng Anh, tiếng Trung, tiếng Việt cùng ý rơi vào cùng một cụm. Gom cụm bằng HDBSCAN chạy mỗi tuần; LLM chỉ đặt tên cụm.

Một cụm được coi là đang lên khi trong 14 ngày có ít nhất 3 kênh khác nhau, mỗi kênh có ít nhất một video S1 từ 2 trở lên với baseline đầy đủ (không tính video baseline\_approx). Yêu cầu nhiều kênh để loại trường hợp một video viral nhờ sức mạnh riêng của kênh đó.

**S5 — Khoảng trống ngôn ngữ.** Cụm đang lên ở kênh tiếng Anh hoặc tiếng Trung, nhưng trong 90 ngày có ít hơn 2 video tiếng Việt thuộc cụm đó trong danh sách theo dõi. Đây là loại cơ hội ưu tiên số 1.

**S6 — Bão hòa.** Cụm có nhiều video tiếng Việt mới nhưng S1 trung vị giảm dần: cảnh báo "đã trễ nhịp", không chọn trừ khi có góc mới rõ ràng.

### 8.3 Phân tích bình luận

Chỉ chạy khi derived\_metrics\_enabled = true; văn bản bình luận và dữ liệu suy ra từ nó theo quy tắc lưu trữ R3. Khi đó, chỉ lấy bình luận của video nổi trội (S1 từ 2 trở lên hoặc comment/view cao) để tiết kiệm quota và chi phí LLM.

1. **Thu thập:** commentThreads.list, order = relevance, tối đa 100 bình luận mỗi video. Không lưu tên hiển thị; ID tác giả mã hóa một chiều (R8).
2. **Phát hiện ngôn ngữ** bằng thư viện, không dùng LLM.
3. **Phân loại** theo lô bằng model rẻ với structured output: question, request (muốn xem chủ đề gì tiếp), disagreement, personal\_story, praise, spam. Kèm chủ đề ngắn bằng tiếng Việt.
4. **Gom cụm** các question và request giữa nhiều video, nhiều ngôn ngữ (bge-m3).
5. **Output cho Research:** "người xem đang hỏi gì", mỗi mục có số bình luận, số video, ngôn ngữ, và câu diễn đạt lại (không trích nguyên văn).
6. **Eval:** 200 bình luận gán nhãn tay (EN, ZH, VI), đo độ chính xác phân loại theo từng lớp.

### 8.4 Quota

YouTube Data API có ba nhóm quota tách riêng ([Quota cost](https://developers.google.com/youtube/data/determine_quota_cost)). search.list có bucket riêng, mặc định 100 lần gọi/ngày, mỗi lần 1 đơn vị. videos.insert có bucket riêng, 100 lần gọi/ngày. Mọi endpoint còn lại dùng chung 10.000 đơn vị/ngày.

Ước tính nhóm chung hằng ngày với 90 kênh: playlistItems khoảng 90 đơn vị, videos.list khoảng 20–40 đơn vị (50 video mỗi lần gọi), commentThreads khoảng 30–60 đơn vị (chỉ khi cờ bật). Tổng dưới 500 đơn vị/ngày. Bảng quota\_usage ghi từng lần gọi theo bucket (search, upload, default) và cảnh báo khi một bucket dùng quá 80% hạn mức ngày.

## 9. Kho tri thức kinh điển (canon)

Canon là nguồn duy nhất của phần "nguyên tác" trong kịch bản. Nó dùng lại kỹ thuật RAG có trích dẫn của RegScan và pgvector của YT\_ELT. Chỉ nạp văn bản gốc hoặc bản dịch đã hết bản quyền; mỗi nguồn phải được xác minh năm xuất bản và license trước khi nạp (backlog C-01).

| Tác phẩm | Văn bản dự kiến nạp | Nguồn dự kiến | Đơn vị cắt |
| --- | --- | --- | --- |
| Marcus Aurelius, Suy tưởng | Bản dịch tiếng Anh George Long (1862) | Project Gutenberg | Quyển.đoạn (4.3) |
| Seneca, Thư gửi Lucilius | Bản dịch Gummere (1917–1925) | Wikisource | Thư.đoạn |
| Seneca, Về sự ngắn ngủi của đời người, Về cơn giận | Văn bản Latin gốc; bản Basore (1928–1935) cần kiểm từng tập vì các tập sau có thể chưa hết hạn ở Mỹ | Wikisource Latin | Chương.đoạn |
| Epictetus, Cẩm nang | Bản dịch Higginson (1865) | Project Gutenberg | Mục |
| Lão Tử, Đạo Đức Kinh | Nguyên văn Hán + bản Legge (1891) để đối chiếu | Wikisource tiếng Trung, Project Gutenberg | Chương (1–81) |
| Trang Tử | Nguyên văn Hán + bản Legge (1891) | Wikisource tiếng Trung | Thiên.đoạn |
| Khổng Tử, Luận Ngữ | Nguyên văn Hán + bản Legge (1861) | Wikisource tiếng Trung | Thiên.chương |
| Tôn Tử, Binh pháp | Nguyên văn Hán + bản Lionel Giles (1910) | Wikisource, Project Gutenberg | Thiên.câu |
| Aesop | Bản Townsend (1867) | Project Gutenberg | Mỗi truyện |
| La Fontaine | Nguyên văn Pháp | Wikisource tiếng Pháp | Mỗi bài |
| Truyện cổ tích Việt Nam | Không nạp tuyển tập hiện đại (còn bản quyền). Người viết tóm tắt cốt truyện dân gian bằng lời mình, đánh dấu license = own | Tự soạn | Mỗi truyện |

Không dùng bản dịch tiếng Việt của người khác nếu chưa chắc đã hết bản quyền. Thay vào đó, kênh tự dịch và ghi "bản dịch của kênh".

**Schema passage:** passage\_id (ví dụ meditations.long1862.4.3), work, author, translator, pub\_year, language, license (public\_domain, own), source\_url, ref\_label hiển thị ("Suy tưởng, quyển 4, đoạn 3"), text, text\_norm, embedding (bge-m3), align\_group (nối nguyên văn Hán với bản Legge cùng chương).

**Truy xuất:** hybrid vector + full-text, gộp bằng RRF như YT\_ELT. Nhờ bge-m3 đa ngôn ngữ, câu hỏi tiếng Việt ("kiểm soát cơn giận") tìm được đoạn tiếng Anh, Latin hoặc Hán. Full-text dùng cấu hình phù hợp từng ngôn ngữ; tránh lỗi đã gặp ở RegScan (index tạo với cấu hình khác với câu truy vấn).

**Bản dịch của kênh:** Writer dịch đoạn cần dùng sang tiếng Việt. Có hai kiểm tra tách riêng và ghi riêng trong CheckReport. (1) **Kiểm citation chính xác:** đoạn trích bằng ngôn ngữ gốc phải khớp passage nguồn sau chuẩn hóa; làm bằng code, fail thì chặn job. (2) **Đánh giá bản dịch:** đo bản tiếng Việt có giữ đúng nghĩa hay không bằng dịch ngược và LLM-judge, ra điểm và nhận xét. Không bao giờ so khớp chính xác, vì một đoạn có nhiều cách dịch đúng; điểm dưới ngưỡng thì gắn cảnh báo. Người vẫn duyệt bản dịch ở bước kịch bản; bản đã duyệt lưu vào canon\_translations để dùng lại nhất quán giữa các video.

**Chống câu trích bịa:** câu nào trong kịch bản gắn tên tác giả mà không khớp passage trong canon thì fail. Giữ thêm danh sách các câu nổi tiếng bị gán sai để Checker cảnh báo khi Research hoặc bình luận mang chúng vào.

## 10. Pipeline sản xuất

Phần thân video dùng ảnh tĩnh có chuyển động nhẹ cộng TTS; clip AI chỉ dùng cho 3–5 giây mở đầu. Cách này giữ chi phí thấp và phong cách ổn định. Mọi bước là một task Python chạy được lại (idempotent), đầu ra lưu trên MinIO theo job\_id.

1. **Giọng đọc (TTS):** mỗi đoạn kịch bản thành một file âm thanh. Ứng viên: [FPT.AI TTS](https://docs.fpt.ai/docs/en/speech/documentation/text-to-speech), [ElevenLabs tiếng Việt](https://elevenlabs.io/zh/text-to-speech/vietnamese). Chọn bằng bài nghe mù 10 đoạn (backlog M-01). Một giọng cố định cho cả kênh; không clone giọng người thật.
2. **Mốc thời gian:** lấy timestamp từng từ nếu nhà cung cấp trả về; nếu không, dùng forced alignment (WhisperX hoặc tương đương) trên file âm thanh và lời đọc. Thời lượng cảnh lấy theo âm thanh thật, không theo ước tính.
3. **Ảnh cảnh:** mỗi cảnh một ảnh. Prompt = tiền tố phong cách cố định của chuyên mục + mô tả cảnh + tiêu cực cố định (không chữ, không logo, không nhân vật có chủ). Nhà cung cấp chọn ở backlog M-02.
4. **Chuyển động:** zoom và pan chậm bằng FFmpeg (zoompan), chuyển cảnh bằng xfade.
5. **Clip mở đầu:** 3–5 giây bằng model video rẻ (Veo 3.1 Lite 720p hoặc Hailuo 2.3). Lỗi hoặc vượt trần chi phí thì dùng ảnh cảnh đầu có chuyển động.
6. **Phụ đề:** sinh từ lời đọc và mốc thời gian, xuất SRT; Shorts đốt chữ vào hình bằng ASS, font hỗ trợ đầy đủ dấu tiếng Việt và có license mở (ví dụ Be Vietnam Pro).
7. **Nhạc nền:** chỉ dùng nhạc có license rõ (ví dụ YouTube Audio Library), lưu license cùng file; tự giảm âm lượng nhạc khi có giọng đọc.
8. **Chuẩn hóa âm lượng:** FFmpeg loudnorm, mục tiêu khoảng -14 LUFS.
9. **Render:** Short 1080×1920, video dài 1920×1080, H.264 preset veryfast.
10. **Thumbnail (video dài):** 3 phương án từ ảnh cảnh + chữ ngắn; người chọn.
11. **Đóng gói MediaBundle:** mp4, srt, thumbnail, metadata.json (tiêu đề, mô tả có nguồn nguyên tác, ghi chú có dùng AI, link affiliate, tags, cờ containsSyntheticMedia, cờ selfDeclaredMadeForKids).
12. **QC kỹ thuật:** Checker dùng ffprobe kiểm độ dài, độ phân giải, âm lượng, phụ đề.
13. **Đăng:** giai đoạn đầu người tự tải lên YouTube Studio, rồi nhập youtube\_video\_id vào hộp thư duyệt; hệ thống lưu vào publications với published\_via = manual để Analyst theo dõi được. Từ P7: upload qua API với privacyStatus = private, người chuyển sang public.

**Chi phí mỗi video** = tổng LLM + TTS + ảnh + clip, ghi theo từng lần gọi. Trần mặc định: 3 USD cho Short, 10 USD cho video dài (cấu hình được). Phần biết chắc: clip 5 giây Veo 3.1 Lite 720p là 0,25 USD. Giá TTS, ảnh và LLM điền sau khi chọn nhà cung cấp; sau 5 video thử sẽ có số thật.

## 11. Dữ liệu, schema và cấu trúc repo

Giữ một repo YT\_ELT và một Postgres (pgvector). Thêm migration từ 008; không sửa migration cũ.

| Migration | Bảng hoặc thay đổi | Nội dung chính |
| --- | --- | --- |
| 008\_channels\_ext | channels | Thêm language (en, zh, vi), pillar (stoic, east, fable), active, added\_by |
| 009\_signals | video\_signals, topic\_clusters, topic\_cluster\_members | S1–S3 theo ngày kèm số thô, format\_guess và cờ baseline\_approx; cụm theo tuần với cờ is\_rising, is\_gap, is\_saturated. Chỉ ghi khi derived\_metrics\_enabled = true |
| 010\_comment\_insights | comment\_labels, viewer\_questions | Nhãn, ngôn ngữ, chủ đề ngắn; câu hỏi đã gom cụm kèm số đếm. Chỉ ghi khi cờ bật |
| 011\_canon | canon\_works, canon\_passages, canon\_translations | Schema mục 9, license bắt buộc NOT NULL |
| 012\_studio\_jobs | jobs, job\_events, approvals | Trạng thái job mục 7.1; mỗi quyết định duyệt có người, thời điểm, lý do và human\_effort\_minutes (thời gian người thực sự làm ở bước đó, không tính thời gian chờ) |
| 013\_studio\_content | topic\_briefs, scripts, script\_segments, scene\_plans, check\_reports | Lưu từng phiên bản, không ghi đè |
| 014\_studio\_media | media\_jobs, media\_bundles, publications | Chi phí từng lần gọi; publications lưu youtube\_video\_id, published\_via (manual hoặc api), published\_at |
| 015\_learning\_ops | learnings, quota\_usage; llm\_calls thêm job\_id, agent; bảng mới chứa API Data có fetched\_at, data\_class (bảng hiện có làm ở P0-07) | Vòng phản hồi, đo chi phí theo agent, quota theo bucket (search, upload, default), xóa dữ liệu theo R3 |

Checkpoint của LangGraph dùng bảng riêng do thư viện checkpoint Postgres tạo. Phân quyền DB: role yti\_assistant hiện có vẫn chỉ đọc; thêm role studio\_app chỉ ghi vào bảng studio và canon\_translations.

**Cấu trúc repo (phần thêm)**

```
src/youtube_intel/
  signals/        s1_outlier.py, s2_velocity.py, s3_engagement.py, clustering.py, gaps.py
  comments/       collect.py, classify.py, cluster.py
src/canon/        ingest.py, chunk.py, retrieve.py, translations.py
src/studio/
  schemas.py      TopicBrief, Script, ScenePlan, MediaBundle, CheckReport, Learning
  state.py        trạng thái graph
  graph.py        LangGraph: node, cạnh, interrupt
  agents/         producer.py, research.py, report.py, writer.py, production.py, checker.py, analyst.py
  production/     tts.py, images.py, video_clip.py, compose.py, subtitles.py, audio.py
  checks/         citations.py, similarity.py, policy.py, media_probe.py
  gateway.py      định tuyến model theo việc, giới hạn chi phí
  mcp/            research_server.py, canon_server.py, assets_server.py
  api.py          FastAPI: tạo job, duyệt, xem trạng thái
dags/             yt_signals.py, yt_comments.py, canon_ingest.py, studio_analytics.py
dashboard/pages/  inbox.py (hộp thư duyệt), briefs.py, jobs.py, costs.py
config/studio.yaml   chuyên mục, style guide, trần chi phí, bảng định tuyến model, ngưỡng tín hiệu
evals/studio/     brief_cases.yaml, script_cases.yaml, comment_labels.csv, checker_cases.yaml
```

Không commit secret, khóa API hay file media vào git; media nằm trên MinIO, khóa nằm trong .env, chỉ commit .env.example.

## 12. Kiểm định, eval, observability và chi phí

Mỗi agent có bộ eval riêng chạy được offline, giống cách YT\_ELT đang chấm trợ lý (tách dev và holdout). Không đổi prompt hay model nếu chưa chạy lại eval và lưu kết quả vào evals/results.

| Bộ eval | Dữ liệu | Chỉ số | Ngưỡng ban đầu |
| --- | --- | --- | --- |
| Grounding số liệu (có sẵn, mở rộng) | 45 ca hiện có + ca hoán đổi thực thể, sai hạng, delta âm | Tỉ lệ bắt đúng lỗi | 100% với các ca G-01, G-02 |
| RAG YT\_ELT | 25 câu, gán nhãn relevant | Recall@5, MRR | Đặt sau khi gán nhãn |
| Truy xuất canon | 40 câu tiếng Việt → passage\_id đúng | Recall@5 | 0,8 |
| Phân loại bình luận (chỉ khi cờ bật) | 200 bình luận EN, ZH, VI gán tay; chỉ lưu comment\_id và nhãn, văn bản lấy lại qua API khi chạy eval (R3), bình luận đã bị xóa thì loại | F1 theo lớp | 0,75 cho question và request |
| TopicBrief | 20 cửa sổ dữ liệu trong hạn lưu trữ (R3) | Mọi số có căn cứ; người chấm hữu ích 1–5 | 100% căn cứ; trung bình từ 3,5 |
| Kịch bản | 20 brief | Citation khớp chính xác passage; đủ format; LLM-judge đã hiệu chỉnh theo điểm người | 100% citation khớp; judge lệch người tối đa 1 điểm |
| Bản dịch tiếng Việt | 30 đoạn kinh điển có bản dịch đã được người chấm | Điểm ngữ nghĩa của LLM-judge (có dịch ngược) so với điểm người | Lệch người tối đa 1 điểm trên 80% số mẫu |
| Checker | 30 kịch bản cài lỗi sẵn (câu trích bịa, tên có chủ, lời khuyên y tế, thiếu bài tập) | Recall lỗi | 0,9 |

**Hiệu chỉnh LLM-judge:** người chấm tay 20 kịch bản đầu theo thang 1–5 cho mạch kể, góc nhìn riêng, độ chính xác. Judge chỉ được dùng tự động khi lệch người tối đa 1 điểm trên 80% số mẫu.

**Observability:** mỗi bước là một span (OpenTelemetry), gắn job\_id và agent; có thể xem trên Langfuse như RegScan. Bảng llm\_calls và media\_jobs là nguồn số liệu chính cho dashboard chi phí.

**Dashboard vận hành** hiển thị, theo agent và theo tuần: chi phí, thời gian, tỉ lệ Checker fail, tỉ lệ người bác, tổng human\_effort\_minutes mỗi video.

**Gateway và chi phí:** gateway.py đọc bảng định tuyến trong config/studio.yaml (việc nào dùng model nào), ghi chi phí mỗi lần gọi, và từ chối lần gọi làm job vượt trần. Model rẻ cho phân loại, nghiên cứu, báo cáo; model tốt cho kịch bản và kiểm định. Trần tháng toàn hệ thống là câu hỏi mở Q1.

## 13. Công nghệ và lý do chọn

Nguyên tắc: chỉ thêm công nghệ khi có vấn đề cụ thể cần giải. Cột cuối cho biết bạn đã biết hay sẽ học mới.

| Công nghệ | Giải vấn đề gì | Không dùng thì sao | Đã biết hay học mới |
| --- | --- | --- | --- |
| Airflow 3 (giữ) | Thu thập, tính tín hiệu, nạp canon theo lịch | Tự viết cron và retry | Đã biết |
| Postgres + pgvector (giữ) | Một nơi cho dữ liệu, vector, trạng thái job, checkpoint | Thêm DB vector riêng, khó join | Đã biết |
| MinIO (giữ) | Lưu media và dữ liệu thô | Ổ đĩa rời rạc | Đã biết |
| LangGraph | Luồng nhiều ngày, dừng chờ người duyệt (interrupt), checkpoint, quay lại bước trước | Tự viết state machine và lưu trạng thái | Đã dùng ở FinSight; học thêm interrupt và checkpoint Postgres |
| MCP | Dùng chung tool Research, Canon, Assets cho nhiều agent và cả Claude hoặc IDE của bạn (N + M thay vì N × M) | Mỗi agent viết tool riêng | Đã dùng ở RegScan; học thêm viết server Python |
| FastAPI | API tạo job, duyệt, xem trạng thái | Dashboard phải gọi thẳng DB | Đã biết (FinSight) |
| Streamlit (giữ) | Hộp thư duyệt, xem brief, chi phí | Duyệt qua file | Đã biết |
| bge-m3 | Gom cụm và truy xuất xuyên ngôn ngữ EN, ZH, VI, Latin | Chủ đề cùng ý nhưng khác ngôn ngữ bị tách cụm | Đã dùng ở RegScan |
| HDBSCAN | Gom cụm không cần biết trước số cụm, có nhiễu | Phải đoán k của k-means | Học mới |
| Pydantic structured output | Output agent đúng schema, kiểm được bằng code | Parse text, dễ vỡ | Đã biết (Zod ở RegScan) |
| FFmpeg | Ghép ảnh, chuyển động, phụ đề, âm lượng, render | Phần mềm dựng tay | Học mới |
| API TTS, ảnh, video | Giọng đọc và hình ảnh | Không có video | Học mới |
| WhisperX hoặc tương đương | Mốc thời gian từ cho phụ đề khi TTS không trả về | Phụ đề lệch | Học mới |
| OpenTelemetry + Langfuse | Trace từng agent, chi phí, lỗi | Mù khi có sự cố | Đã dùng ở RegScan |
| YouTube Analytics API | Retention, nguồn traffic của kênh mình | Không có vòng phản hồi | Học mới (OAuth) |

**Không dùng:** n8n (luồng đã nằm trong Airflow và LangGraph, thêm một hệ nữa chỉ tăng chỗ hỏng); CrewAI hoặc AutoGen (ít kiểm soát điểm duyệt hơn LangGraph); Kubernetes (một máy Docker Compose là đủ).

## 14. Backlog có ID và tiêu chí nghiệm thu

Làm theo thứ tự giai đoạn. Mỗi mục xong khi đạt tiêu chí nghiệm thu, có test, và kết quả eval (nếu có) đã lưu vào evals/results.

### P0 — Đóng YT\_ELT hiện tại (trước 06/11/2026)

| ID | Việc | Tiêu chí nghiệm thu |
| --- | --- | --- |
| P0-01 | Sửa G-01: grounding gắn số với đúng thực thể và thứ hạng | Hoán đổi số giữa video A và B, hoặc sai hạng, cho ok = False; test mới pass |
| P0-02 | Sửa G-02: chiều tăng, giảm và dấu | Delta -1200 với "giảm 1.200" được chấp nhận; với "tăng 1.200" thì bị từ chối |
| P0-03 | Gán nhãn relevant cho 25 câu RAG, chạy eval, chọn lại rag.min\_similarity | Kết quả trong evals/results; ngưỡng mới có lý do ghi trong file |
| P0-04 | Nối cờ derived\_metrics\_enabled vào code, chặn đúng các tính năng nâng cao ở mục 8 | Cờ false (mặc định): không tính, không lưu, không trả S1–S6, cụm chủ đề, phân loại hay cảm xúc bình luận, so sánh giữa các kênh; chỉ còn số thô, sắp xếp, lọc, mức tăng view của từng video. Mỗi tính năng bị chặn có một test |
| P0-05 | Chạy live, chạy 45 ca eval trợ lý | Báo cáo dev và holdout lưu trong evals/results |
| P0-06 | Thay đặc tả cũ bằng tài liệu này, cập nhật README theo hướng studio | File cũ bị thay; README có mục "Hướng phát triển" |
| P0-07 | Áp quy tắc lưu trữ R3 vào retention.py; thêm fetched\_at, data\_class cho các bảng hiện có | Với cờ tắt: snapshot thô, thống kê Non-Authorized, metadata, văn bản bình luận và embedding suy ra từ chúng bị xóa hoặc làm mới sau tối đa 30 ngày; có test cho từng lớp dữ liệu |

### P1 — Research mở rộng

| ID | Việc | Tiêu chí nghiệm thu |
| --- | --- | --- |
| R-01 | Migration 008; channels.yaml thêm language, pillar, active; 60–90 kênh do người chốt | DAG thu thập chạy hết danh sách; quota\_usage ghi theo bucket, nhóm chung dưới 500 đơn vị/ngày; search.list chỉ gọi khi tìm kênh mới |
| R-02 | Tín hiệu S1–S3 (migration 009), chỉ khi cờ bật | S1 so cùng tuổi và cùng định dạng; baseline\_approx gắn đúng và video baseline\_approx không bao giờ được gắn "nổi trội"; luôn lưu kèm số thô; unit test trên dữ liệu mẫu |
| R-03 | Nhúng bge-m3, gom cụm HDBSCAN theo tuần, S4–S6 (chỉ khi cờ bật) | 20 cặp video cùng ý khác ngôn ngữ: ít nhất 16 cặp vào cùng cụm; 10 cụm kiểm tay: ít nhất 8 cụm nhất quán |
| R-04 | Phân tích bình luận (migration 010, chỉ khi cờ bật) | F1 từ 0,75 cho question và request trên 200 nhãn; không lưu tên hiển thị |
| R-05 | Agent Research xuất TopicBrief | 20 brief eval: 100% số liệu có căn cứ; người chấm hữu ích trung bình từ 3,5; với cờ tắt, brief vẫn chạy được chỉ với số thô và mức tăng view |
| R-06 | Agent Report: bản tin thị trường hằng ngày | Chạy 7 ngày liên tục; mọi bản tin qua grounding |

### P2 — Canon

| ID | Việc | Tiêu chí nghiệm thu |
| --- | --- | --- |
| C-01 | Xác minh năm xuất bản và license từng nguồn mục 9 | Bảng nguồn đủ năm, URL, license; nguồn chưa xác minh không được nạp |
| C-02 | Nạp và cắt theo đơn vị tự nhiên (migration 011) | 30 passage kiểm tay: ref\_label đúng 100% |
| C-03 | Truy xuất hybrid đa ngôn ngữ | Recall@5 từ 0,8 trên 40 câu tiếng Việt |
| C-04 | MCP canon\_server (search\_canon, get\_passage) | Gọi được từ một MCP client bên ngoài; có test |

### P3 — Writer và Checker

| ID | Việc | Tiêu chí nghiệm thu |
| --- | --- | --- |
| W-01 | Style guide 3 chuyên mục (giọng, độ dài, tiền tố hình ảnh, điều cấm) | Người duyệt; lưu trong config/studio.yaml |
| W-02 | Agent Writer xuất Script + ScenePlan | 20 brief eval: 100% citation khớp passage; đủ 3 phần format |
| W-03 | Agent Checker đủ các kiểm tra mục 7.6 | Recall lỗi từ 0,9 trên 30 kịch bản cài lỗi; kiểm citation chính xác và đánh giá bản dịch ra hai kết quả riêng |
| W-04 | Hiệu chỉnh LLM-judge | Lệch người tối đa 1 điểm trên 80% của 20 mẫu |

### P4 — Production

| ID | Việc | Tiêu chí nghiệm thu |
| --- | --- | --- |
| M-01 | Chọn TTS bằng bài nghe mù 10 đoạn | Kết quả và giá ghi trong docs/decisions |
| M-02 | Chọn nhà cung cấp ảnh; tiền tố phong cách từng chuyên mục | 10 ảnh mỗi chuyên mục, người duyệt là nhất quán |
| M-03 | Ghép video bằng FFmpeg (zoompan, xfade, phụ đề, loudnorm) | Render từ ScenePlan mẫu; ffprobe đạt độ dài, độ phân giải, âm lượng |
| M-04 | Clip mở đầu và dự phòng | API lỗi thì dùng ảnh dự phòng, job không fail |
| M-05 | Ghi chi phí media\_jobs và trần chi phí | Job sắp vượt trần dừng trước lần gọi gây vượt |
| M-06 | Mốc thí điểm: 5 video làm bán thủ công, chạy từng bước bằng CLI | Ghi human\_effort\_minutes, chi phí, phần bị sửa nhiều nhất cho từng video |

### P5 — Điều phối

| ID | Việc | Tiêu chí nghiệm thu |
| --- | --- | --- |
| O-01 | Graph LangGraph, state, checkpoint Postgres | Tắt tiến trình giữa chừng, khởi động lại tiếp tục đúng bước |
| O-02 | 3 điểm duyệt: interrupt + FastAPI + hộp thư Streamlit | Bác kèm lý do thì quay về bước trước và agent nhận được lý do; mỗi lần duyệt ghi human\_effort\_minutes do người nhập |
| O-03 | Producer nhận yêu cầu tự do | 10 yêu cầu mẫu cho job plan đúng |
| O-04 | Trace OpenTelemetry/Langfuse, dashboard chi phí và tỉ lệ bác | Mỗi job xem được toàn bộ span và tổng chi phí |

### P6 — Vòng phản hồi

| ID | Việc | Tiêu chí nghiệm thu |
| --- | --- | --- |
| A-00 | Nhập youtube\_video\_id sau khi đăng thủ công (hộp thư duyệt hoặc API), lưu publications với published\_via = manual | Analyst chạy được với video đăng thủ công trước P7; video\_id phải thuộc kênh đã kết nối OAuth; job chuyển sang published |
| A-01 | OAuth YouTube Analytics cho kênh của mình | Lấy được retention và nguồn traffic từng video |
| A-02 | Agent Analyst chạy sau 7 và 28 ngày, ghi learnings | Mỗi learning có bằng chứng; Research và Writer đọc learnings còn hiệu lực |
| A-03 | Hiệu chỉnh ngưỡng S1–S6 theo kết quả thật (khi cờ bật) | Sau 20 video: báo cáo tín hiệu nào dự đoán đúng, ngưỡng mới ghi lý do |

### P7 — Đăng qua API

| ID | Việc | Tiêu chí nghiệm thu |
| --- | --- | --- |
| U-01 | Upload qua videos.insert với privacyStatus = private, containsSyntheticMedia, selfDeclaredMadeForKids | Video lên ở chế độ private; người chuyển public trong Studio |
| U-02 | Metadata: mô tả có nguồn nguyên tác, ghi chú AI, link affiliate | Checker xác nhận đủ trước khi upload |

## 15. Lộ trình

Trước 06/11/2026 chỉ làm P0. Studio bắt đầu từ tuần 09/11 và đạt nhịp 3 video/tuần khoảng cuối tháng 1/2027.

&#91;embedded content: lộ trình dự kiến · 30/10/2026 đến 21/02/2027\]

P1 và P2 chạy song song vì không phụ thuộc nhau. Đợt thí điểm 5 video chạy bán thủ công ngay khi P3 và P4 có bản dùng được; chỉ tự động hóa bằng LangGraph (P5) những bước đã chạy ổn khi làm tay. Sau quý 1/2027 mới cân nhắc mảng trẻ em, phiên bản tiếng Anh và việc bán công cụ cho creator khác.

## 16. Rủi ro, câu hỏi mở và nguồn

| Rủi ro | Dấu hiệu sớm | Cách xử lý |
| --- | --- | --- |
| Kênh bị coi là nội dung rập khuôn | Video mới giống video cũ (similarity cao), retention thấp đều | Giữ format có góc nhìn riêng, giới hạn số video/tuần, người duyệt kịch bản |
| Câu trích sai hoặc bịa | Checker fail citation; bình luận người xem báo sai | Chỉ trích từ canon; sửa công khai trong mô tả khi có sai |
| Bản quyền bản dịch | Khiếu nại Content ID hoặc bản quyền | C-01 xác minh license; tự dịch thay vì dùng bản dịch của người khác |
| Theo xu hướng bị trễ nhịp | S6 báo bão hòa, video đăng xong chủ đề đã nguội | Ưu tiên S5 (khoảng trống EN/ZH so với VI); giữ 30% video là chủ đề không lỗi thời |
| Chi phí API vượt dự kiến | Dashboard chi phí tuần tăng | Trần theo video và theo tháng trong gateway |
| Chính sách YouTube tiếp tục thay đổi | Thông báo chính sách mới | Luật nằm trong config và Checker, sửa một chỗ; đọc lại chính sách mỗi quý |
| Giọng TTS tiếng Việt kém tự nhiên | Retention giảm mạnh ở 10 giây đầu | Bài nghe mù M-01; thử 2 nhà cung cấp trên 5 video đầu |
| Dự án chiếm thời gian chuẩn bị phỏng vấn | P0 chưa xong gần 06/11 | Trước 06/11 chỉ làm P0 |

**Câu hỏi mở**

- [ ] Q1: Ngân sách hằng tháng cho API (LLM, TTS, ảnh, video) và máy chủ là bao nhiêu?
- [ ] Q2: Chọn nhà cung cấp TTS và ảnh nào (sau M-01, M-02)?
- [ ] Q3: Tên kênh và nhận diện hình ảnh?
- [ ] Q4: Danh sách 60–90 kênh theo dõi ban đầu (agent đề xuất, người chốt)?
- [ ] Q5: Có làm phiên bản tiếng Anh từ quý 1/2027 không?
- [ ] Q6: Chạy trên máy cá nhân hay VPS (bge-m3 và WhisperX cần RAM, tốt nhất có GPU)?
- [ ] Q7: Có nộp đơn chấp nhận phần sửa đổi chính sách (use case Analytics & Reporting) để bật derived\_metrics\_enabled không? Chưa được chấp thuận thì studio chạy ở chế độ cơ bản.

**Nguồn**

- [TechCrunch: YouTube clarifies policies around AI slop and upsetting videos (20/07/2026)](https://techcrunch.com/2026/07/20/youtube-clarifies-policies-around-ai-slop-and-upsetting-videos/)
- [YourStory: AI slop on YouTube Kids sparks calls for a ban](https://yourstory.com/ai-story/youtube-ai-slop-kids-ban)
- [OutlierKit: Best Faceless YouTube Niches 2026](https://alpha.outlierkit.com/resources/faceless-youtube-channels/)
- [DevTk: AI Video API Pricing 2026](https://devtk.ai/en/blog/ai-video-generation-pricing-2026/)
- [MiniMatters: YouTube AI Content Labeling Update (05/2026)](https://minimatters.com/?p=6160)
- [YouTube Data API: videos.insert](https://Developers.Google.com/youtube/v3/docs/videos/insert)
- [YouTube API Services Developer Policies](https://developers.google.com/youtube/terms/developer-policies)
- [Additional policies for derived metrics and data storage](https://developers.google.com/youtube/terms/derived-metrics-policy)
- [YouTube Data API: Quota cost](https://developers.google.com/youtube/data/determine_quota_cost)
- [Complying with YouTube's Developer Policies](https://developers.google.cn/youtube/terms/developer-policies-guide?hl=en)
- [FPT.AI Text to Speech](https://docs.fpt.ai/docs/en/speech/documentation/text-to-speech)
- [ElevenLabs Vietnamese TTS](https://elevenlabs.io/zh/text-to-speech/vietnamese)
