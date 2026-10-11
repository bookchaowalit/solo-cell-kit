---
title: solo-cell-kit — Product Card
doc_id: CARD-solo-cell-kit
doc_type: product-card
verdict: template
version: 0.1
date: 2026-10-11
status: ร่างเพื่อพิจารณา
owner: Product owner (Book Dev)
---

# solo-cell-kit — Product Card

## 1. สรุปคุณค่าในหนึ่งประโยค

สร้าง repo, HTTP API, schema Postgres (Neon), write path ผ่าน platform, graph outbox, ไฟล์ deploy และ contract test ของ cell ใหม่จากไฟล์ spec เดียว

## 2. ปัญหาและผู้ใช้เป้าหมาย

การสร้าง cell ด้วยมือซ้ำ ๆ ทำให้ rule ไม่ตรงกัน ผู้ใช้คือเจ้าของและ agent ที่เปิด domain ใหม่

## 3. สิ่งที่ทำได้จริงตอนนี้

- `cellkit-new <dir>` เขียน repo ใหม่ และ `--sync` รีเฟรช kit ที่ vendor ไว้ใน `src/cellkit`
- `cellkit/spec.py` (รูปแบบ spec), `cellkit/app.py` (routes), `cellkit/contract.py` (สิ่งที่ทุก cell ต้องพิสูจน์)
- ตัวอย่าง `examples/demo` และ templates; เทสต์ 4 ไฟล์

## 4. หลักฐานสถานะ

| รายการ | ค่า |
|---|---|
| Stack | FastAPI, Python |
| Commit | 6 ครั้ง (2026-10-10 ถึง 2026-10-11) |
| ซอร์สโค้ด | 16 ไฟล์ / 2641 บรรทัด |
| เทสต์ | 4 ไฟล์ (ไม่ได้รันในงานนี้) |
| CI | ไม่มี workflow |
| Deploy config | ไม่มี (เป็น generator) |
| Live URL | ไม่มี |

## 5. คุณค่าและจุดต่าง

- ทำให้ cell ทั้ง 10+ ตัวเดินตามกติกาเดียวกัน (สิทธิ์ RLS execution outbox)
- **[สมมติฐาน]** ใช้เป็นแม่แบบ cell ให้ผู้อื่นหรือขายเป็น SaaS builder

## 6. ช่องว่างสำคัญ

- สร้างแล้ว sync เป็นสำเนา (vendored) — cell เก่าต้อง `--sync` เองเมื่อ kit เปลี่ยน
- ผูกกับ empire-platform ยังไม่มีผู้ใช้ภายนอก

## 7. คำตัดสิน

**`template`** — เป็นแม่แบบ/generator สำหรับสร้าง cell ใหม่ ไม่ใช่ผลิตภัณฑ์ที่ขายตรง จะเปลี่ยนเป็น `invest` หากเจ้าของตัดสินใจเปิดให้ผู้อื่นใช้สร้าง cell

## 8. ขั้นต่อไป

1. เก็บทะเบียนว่า cell ใดอยู่ kit เวอร์ชันใด
2. เพิ่ม CI รัน `pytest` บน examples/demo

## การอนุมัติ

| บทบาท | สถานะ | วันที่ | ลงนาม |
|---|---|---|---|
| Product owner (Book Dev) | รอพิจารณา | — | ________ |

## แหล่งข้อมูล

- `README.md`
- `cellkit/`, `examples/demo`, `tests/`
