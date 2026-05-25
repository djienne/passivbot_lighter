# Headless AI Code Tasks with Codex CLI & Gemini CLI

Quick reference for running headless code tasks (reviews, investigations, debugging, refactoring analysis) using OpenAI Codex CLI and Google Gemini CLI.

## Prerequisites

```bash
# Install
npm install -g @openai/codex
# Gemini CLI: install per Google's instructions

# Authenticate
codex login          # Opens browser for ChatGPT OAuth
gemini               # First run triggers auth flow
```

## OpenAI Codex CLI

### Available Models (ChatGPT account)
| Model | Use case |
|-------|----------|
| `gpt-5.4` | Flagship, best for deep analysis |
| `gpt-5.4-mini` | Faster, lighter tasks |

### Config (`~/.codex/config.toml`)
```toml
model = "gpt-5.4"
model_reasoning_effort = "xhigh"    # low, medium, high, xhigh
```

### Headless Mode (non-interactive)

```bash
# Basic: run a prompt, save output
codex exec -m gpt-5.4 --sandbox read-only \
  -o results/output.md \
  "Your prompt here"

# Code review
codex exec -m gpt-5.4 --sandbox read-only \
  -o results/review.md \
  "Review scripts/trader/executor_live.py for bugs. \
   Focus on: rounding errors, race conditions, unchecked API responses. \
   Report each finding with file:line, severity, and suggested fix."

# Bug investigation
codex exec -m gpt-5.4 --sandbox read-only \
  -o results/investigation.md \
  "Find which test(s) in tests/ write to results/live_trades.csv. \
   Report exact file:line for each offender and suggested fix."

# Architecture analysis
codex exec -m gpt-5.4 --sandbox read-only \
  -o results/analysis.md \
  "Analyze the data flow from SSVI calibration to trade entry. \
   Identify bottlenecks and single points of failure."

# Review git diff
codex exec review --base main \
  "Focus on security and error handling"

# Review uncommitted changes
codex exec review --uncommitted
```

### Monitoring Codex Output

**Important:** Codex `-o` writes the output file only at process exit. On Windows, the process can take 10-20 minutes for deep analysis and may hang after completion.

```bash
# Run with tee to stream live log while also writing -o file
codex exec -m gpt-5.4 --sandbox read-only \
  -o results/output.md \
  "Your prompt here" 2>&1 | tee /tmp/codex_live.log

# Monitor progress in another terminal
tail -f /tmp/codex_live.log

# Check if -o file was written (only appears at end)
ls -la results/output.md

# If process hangs after writing output, kill it
tasklist | grep codex        # Find PID
taskkill //PID <pid> //F     # Windows
kill <pid>                   # Linux/Mac
```

### Key Flags
| Flag | Purpose |
|------|---------|
| `-m gpt-5.4` | Select model |
| `--sandbox read-only` | Prevent file modifications |
| `-o file.md` | Save final response to file (writes at end only) |
| `--json` | Output as JSONL (for parsing) |
| `--full-auto` | Auto-approve reads (workspace-write sandbox) |
| `-C /path` | Set working directory |
| `--ephemeral` | Don't persist session to disk |

## Google Gemini CLI

### Headless Mode (non-interactive)

```bash
# Basic prompt mode (-p flag)
gemini -p "Your prompt here" > results/output.md

# With auto-approve for file reads (--yolo)
gemini --yolo -p "Your detailed prompt here" \
  > results/output.md 2>/dev/null

# With specific model
gemini -m gemini-2.5-pro -p "Your prompt" > output.md
```

### Gemini Gotcha: .gitignore / .geminiignore

Gemini CLI respects `.gitignore` and `.geminiignore` patterns — it **cannot read** files matched by these patterns using its built-in `read_file` or `glob` tools. Workarounds:

```bash
# Option 1: Inline the relevant code/config directly in the prompt
gemini --yolo -p "$(cat <<'EOF'
Here is the config:
$(cat configs/my_config.json)

Review for issues...
EOF
)" > results/output.md 2>/dev/null

# Option 2: Gemini can use run_shell_command to bypass ignore patterns
# (it will figure this out on its own with --yolo, but may waste time first)
```

### Key Flags
| Flag | Purpose |
|------|---------|
| `-p "prompt"` | Non-interactive (headless) mode |
| `--yolo` | Auto-approve all tool use (file reads etc.) |
| `-m model` | Select model |
| `--sandbox` | Run in sandbox |
| `--approval-mode plan` | Read-only mode |

## Running Both in Parallel

Run both tools simultaneously and compare findings:

```bash
cd /path/to/project

# Save prompt to file for reuse
cat > /tmp/review_prompt.txt <<'EOF'
Your review prompt here (can be multi-line).
Be specific about files to examine and what to look for.
EOF

# Codex (save to file via -o, stream via tee)
codex exec -m gpt-5.4 --sandbox read-only \
  -o results/codex_output.md \
  "$(cat /tmp/review_prompt.txt)" 2>&1 | tee /tmp/codex_live.log &

# Gemini (save via redirect)
gemini --yolo -p "$(cat /tmp/review_prompt.txt)" \
  > results/gemini_output.md 2>/dev/null &

# Wait for both
wait
echo "Both complete"
```

## Prompt Tips

- **Be specific** — list exact files, functions, and what to look for
- **Set scope** — "Focus on X, Y, Z" prevents the model from wandering
- **Request structure** — "Report each finding with file:line, severity, and suggested fix"
- **Provide context** — mention what's already been fixed or investigated
- **State what NOT to do** — "Do NOT compare config files, the config is identical"
- **Use read-only sandbox** to prevent accidental code changes
- **Save output** to `.md` files for later reference
- For large codebases, point the prompt at specific files/directories to stay within context
- If Gemini can't read files due to gitignore, inline the relevant content in the prompt

## Example Prompts

### Code Review
```
Review the live trading execution code for potential bugs that could
cause real money loss. Focus on: executor_live.py, trader.py.
Check for: phantom positions, rounding errors, race conditions.
Report each finding with file:line, severity, and suggested fix.
```

### Bug Investigation
```
Find which test(s) in tests/ write to results/live_trades.csv
(the REAL production files, not tmp_path copies). Report exact
file:line for each offender and suggested fix.
```

### Exchange Connector Comparison
```
IMPORTANT: This is about the SAME config running on TWO DIFFERENT
EXCHANGES. Do NOT compare config files — they are identical.

Compare src/exchanges/exchange_a.py vs src/exchanges/exchange_b.py.
Check for differences in: balance calculation, position reporting,
candle fetching, order execution, and any data fed to the shared engine.
For each finding: file:line, severity, and how it affects behavior.
```

### Architecture Review
```
Analyze how paper and live trading modes share state and resources.
Identify any race conditions, file conflicts, or data corruption
risks when both run in parallel.
```

### Performance Analysis
```
Profile the SSVI surface calibration pipeline. Identify the slowest
steps and suggest optimizations that preserve accuracy.
```
