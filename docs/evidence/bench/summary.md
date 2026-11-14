| Task | Mode | Answered by | Route (reason) | Turns | Quality | Median s | p90 s | Model calls/turn | Est. prompt tokens |
|---|---|---|---|---|---|---|---|---|---|
| T1 Mail extraction (4 emails) | cloud | gpt-5.6-sol | cloud/reasoning_high | 8 | 100% | 3.8 | 4.7 | 1.0 | 10,184 |
| T1 Mail extraction (4 emails) | local | gemma4:e4b | local/privacy_high | 8 | 81% | 0.9 | 2.2 | 1.0 | 10,183 |
| T1 Mail extraction (4 emails) | ollama | local (gemma4:e4b, direct) | – | 8 | 78% | 0.3 | 0.4 | 0.0 | 159 |
| T2 Simple chat (3 prompts) | auto | gemma4:e4b | local/simple_task | 6 | 100% | 0.5 | 0.7 | 1.0 | 9,998 |
| T2 Simple chat (3 prompts) | cloud | gpt-5.6-sol | cloud/reasoning_high | 6 | 100% | 2.5 | 2.9 | 1.0 | 10,007 |
| T3 Data question with tools (2) | auto | gpt-5.6-sol | cloud/agentic_prompt | 4 | 100% | 13.7 | 21.4 | 3.5 | 15,001 |
| T3 Data question with tools (2) | local | gemma4:e4b | local/privacy_high | 4 | 0% | 2.1 | 7.7 | 2.2 | 10,183 |
| T4 Multi-step plan | auto | gpt-5.6-sol | cloud/reasoning_high | 2 | 100% | 34.3 | 35.9 | 4.0 | 20,691 |
| T4 Multi-step plan | local | gemma4:e4b | local/privacy_high | 2 | 0% | 22.6 | 23.5 | 4.5 | 10,441 |
| T5 Long context (needle in ~150K chars) | auto | gpt-5.6-sol | cloud/long_context | 2 | 100% | 4.9 | 6.7 | 1.0 | 60,571 |
| T5 Long context (needle in ~150K chars) | ollama | local (gemma4:e4b, direct) | – | 2 | 100% | 5.2 | 10.1 | 0.0 | 36,545 |

Cold start (Gemma unloaded first): T2 arith answered by gemma4:e4b in 16.95 s (router 5672 ms).
