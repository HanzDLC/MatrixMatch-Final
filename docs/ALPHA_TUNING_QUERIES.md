# Alpha Tuning — Query Set & Ground Truth

Evaluation set for hybrid retrieval (SBERT + BM25) alpha tuning. Each query represents a plausible 3rd-year capstone idea a student might submit. Ground truth doc IDs were hand-picked after reading all 64 abstracts in [matrixmatch.sql](../matrixmatch.sql) (IDs 40–103).

**Goal:** sweep α ∈ [0.00, 1.00] step 0.05 in `final = α·SBERT + (1−α)·BM25_norm`, measure Top-5 Accuracy, MRR, Recall@5 per α, identify optimum.

---

## Query Set (15)

| # | Query Abstract (short form) | Relevant Doc IDs | Domain |
|---|---|---|---|
| Q1  | Web-based inventory management system for a small business with stock alerts and sales reporting. | 44, 70, 71, 83 | Web / Inventory |
| Q2  | IoT-based smart agriculture system that monitors soil moisture and automates irrigation. | 99, 88 | IoT / Agriculture |
| Q3  | Mobile app with GPS tracking for public transportation commuters. | 66, 81, 58 | Mobile / Transit |
| Q4  | AI chatbot for answering student inquiries about enrollment and academic policies. | 49, 91 | AI / Chatbot |
| Q5  | Scheduling algorithm for optimizing university class assignments. | 64, 46 | Scheduling |
| Q6  | Library management system using QR codes for book borrowing and returns. | 41, 74 | Library / QR |
| Q7  | Augmented reality educational app using CNN for object recognition. | 48, 57, 59 | AR / CNN |
| Q8  | Sign language translator using computer vision and machine learning. | 54, 94 | CV / Accessibility |
| Q9  | Video content filtering system for detecting inappropriate material. | 50, 56 | Video / Filtering |
| Q10 | Online ferry ticketing and reservation platform. | 43, 103 | Booking / Transport |
| Q11 | Sentiment analysis of social media posts or student feedback. | 69, 97 | NLP / Sentiment |
| Q12 | Online marketplace connecting local producers with consumers. | 40, 47 | E-commerce |
| Q13 | Gamified learning application for elementary students. | 85, 98, 59 | EdTech / Gamification |
| Q14 | IoT real-time environmental monitoring dashboard. | 87, 99, 101 | IoT / Monitoring |
| Q15 | Crowdfunding platform for disaster relief and community projects. | 86, 95 | Web / Crowdfunding |

---

## Deliberate False-Positive Lures

These queries intentionally sit near ambiguous docs so the ranking must discriminate:

- **Q1 (inventory)** — may pull *ProductLoca* (finder, not inventory).
- **Q7 (AR + CNN)** — may pull *ISATour* (tourism app, not AR-educational).
- **Q3 (transit GPS)** — may pull unrelated mobile-GPS apps.
- **Q13 (gamified learning)** — Q7's AR educational docs overlap.

---

## Domains NOT in Repo (avoid writing queries for these)

- Tourism / travel-booking apps
- ML for student dropout prediction
- Genetic algorithms specifically
- Healthcare / EMR systems
- Blockchain / crypto

---

## Next Steps

1. **User sanity-check**: open each ground truth doc ID above and confirm relevance. Adjust list.
2. Finalize query count (15 as-is / trim to 10 / expand to 18–20).
3. Generate `queries_ground_truth.csv` from this file.
4. Write `alpha_tuning.py` — sweep α, compute Top-5 Acc + MRR + Recall@5, add RRF baseline row.
5. Draft Limitations paragraph for the methodology chapter.
