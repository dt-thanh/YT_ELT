# Studio eval data

Human-made inputs for the P1+ acceptance tests. Only ids and labels are stored here:
public API text (titles, comments) may not be kept longer than 30 days (R3), so the eval
re-reads it through the API when it runs.

| File | Item | Who fills it | Run |
|---|---|---|---|
| `cluster_pairs.yaml` | R-03: 20 same-idea pairs in different languages | owner | `youtube-intel topics --pairs evals/studio/cluster_pairs.yaml` |
| `comment_labels.csv` | R-04: ~200 comments (EN, ZH, VI), one label each: question, request, disagreement, personal_story, praise, spam | owner | `youtube-intel eval-comments` (OpenAI) |
