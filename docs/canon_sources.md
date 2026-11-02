# Nguồn kinh điển (canon) — C-01

Nguồn sự thật: [`config/canon_sources.yaml`](../config/canon_sources.yaml) (bảng này sinh từ đó, 2026-11-02).
Chỉ nguồn `verified` được nạp (`youtube-intel canon-load`); nguồn `excluded` được ghi lại kèm lý do.

Xác minh ngày 2026-11-02 bằng cách mở trang nguồn (Project Gutenberg, Wikisource, The Latin Library,
archive.org, Library of Congress). Quy tắc Việt Nam dùng: đời tác giả/dịch giả + 50 năm. Đây là ghi
chép kỹ thuật, **không phải tư vấn pháp lý** — chủ kênh xác nhận lần cuối.

| id | Tác phẩm | Ngôn ngữ | Năm | License | Trạng thái | URL |
|---|---|---|---|---|---|---|
| `meditations.long1862` | Suy tưởng — Marcus Aurelius / George Long | en | 1862 | public_domain | **verified** | [nguồn](https://www.gutenberg.org/ebooks/15877) |
| `enchiridion.higginson1865` | Cẩm nang — Epictetus / Thomas Wentworth Higginson | en | 1865 | public_domain | **verified** | [nguồn](https://www.gutenberg.org/ebooks/45109) |
| `daodejing.legge1891` | Đạo Đức Kinh — Laozi / James Legge | en | 1891 | public_domain | **verified** | [nguồn](https://www.gutenberg.org/ebooks/216) |
| `analects.legge1861` | Luận Ngữ — Confucius / James Legge | en | 1861 | public_domain | **verified** | [nguồn](https://www.gutenberg.org/ebooks/3330) |
| `artofwar.giles1910` | Binh pháp Tôn Tử — Sun Tzu / Lionel Giles | en | 1910 | public_domain | **verified** | [nguồn](https://www.gutenberg.org/ebooks/17405) |
| `aesop.townsend1867` | Ngụ ngôn Aesop — Aesop / George Fyler Townsend | en | 1867 | public_domain | **verified** | [nguồn](https://www.gutenberg.org/ebooks/21) |
| `letters.gummere1917` | Thư gửi Lucilius — Seneca / Richard Mott Gummere | en | 1917 | public_domain | **verified** | [nguồn](https://en.wikisource.org/wiki/Moral_letters_to_Lucilius) |
| `daodejing.wangbi` | Đạo Đức Kinh — Laozi | zh | 400 TCN (ước tính) | public_domain | **verified** | [nguồn](https://zh.wikisource.org/wiki/%E9%81%93%E5%BE%B7%E7%B6%93_%28%E7%8E%8B%E5%BC%BC%E6%9C%AC%29) |
| `sunzi.wikisource` | Binh pháp Tôn Tử — Sun Tzu | zh | 500 TCN (ước tính) | public_domain | **verified** | [nguồn](https://zh.wikisource.org/wiki/%E5%AD%AB%E5%AD%90%E5%85%B5%E6%B3%95) |
| `lunyu.wikisource` | Luận Ngữ — Confucius | zh | 400 TCN (ước tính) | public_domain | **verified** | [nguồn](https://zh.wikisource.org/wiki/%E8%AB%96%E8%AA%9E) |
| `zhuangzi.wikisource` | Trang Tử — Zhuangzi | zh | 300 TCN (ước tính) | public_domain | **verified** | [nguồn](https://zh.wikisource.org/wiki/%E8%8E%8A%E5%AD%90) |
| `debrevitate.latin` | Về sự ngắn ngủi của đời người — Seneca | la | ~49 (ước tính) | public_domain | **verified** | [nguồn](https://www.thelatinlibrary.com/sen/sen.brevita.shtml) |
| `deira.latin` | Về cơn giận — Seneca | la | ~45 (ước tính) | public_domain | **verified** | [nguồn](https://www.thelatinlibrary.com/sen/sen.ira1.shtml) |
| `lafontaine.fables` | Ngụ ngôn La Fontaine — Jean de La Fontaine | fr | ~1668 (ước tính) | public_domain | **excluded** | [nguồn](https://fr.wikisource.org/wiki/Fables_de_La_Fontaine_(%C3%A9dition_originale)) |
| `moralessays.basore` | Tiểu luận đạo đức (bản dịch Basore) — Seneca / John W. Basore | en | 1928 | public_domain | **excluded** | [nguồn](https://archive.org/details/moralessayswithe01seneuoft) |
| `zhuangzi.legge1891` | Trang Tử (bản dịch Legge) — Zhuangzi / James Legge | en | 1891 | public_domain | **excluded** | [nguồn](https://archive.org/details/wg939) |

## Điểm cần biết

- **Suy tưởng:** ebook Gutenberg #2680 thường bị gọi nhầm là bản George Long; thực ra là bản Meric
  Casaubon (1634). Bản Long là #15877.
- **Seneca, Về sự ngắn ngủi của đời người:** chỉ nạp nguyên văn Latin. Bản dịch Basore tập II (1932)
  in ở Anh, có thể còn bản quyền ở Mỹ tới 2027 (URAA). *De Ira* cũng chỉ nạp Latin (Basore tập I an
  toàn nhưng không có văn bản sạch).
- **Thư gửi Lucilius (Gummere):** passage theo số mục của bản Loeb, Wikisource giữ chúng trong thẻ
  `wst-verse`; phải tải bằng `action=render` vì `action=raw` không có chữ.
- **Luận Ngữ:** cách chia chương của Wikisource lệch bản Legge ở thiên V và IX (mỗi thiên một chương),
  nên ghép nguyên văn – bản dịch có thể lệch một chương ở phần sau hai thiên đó.
- **File nguồn có lỗi đánh số** (Gutenberg Luận Ngữ, Latin Library *De Brevitate*): bộ cắt đếm theo thứ
  tự hoặc ghi nhãn khoảng ("đoạn 4-5") thay vì đoán.
- **Chưa nạp:** La Fontaine (cấu trúc trang không thống nhất), Trang Tử bản Legge (không có bản điện tử).
