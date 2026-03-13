# Academic Writing Assistant Bot

A Python bot that takes your text, finds real academic sources, verifies them, and formats citations in APA, MLA, or Chicago style — with an optional humanizing pass.

## Setup

### 1. Install dependencies
```bash
pip install anthropic requests
```

### 2. Get your Anthropic API key
- Go to https://console.anthropic.com
- Create an API key
- Open `academic_bot.py` and replace `"YOUR_API_KEY_HERE"` with your key

Or set it as an environment variable and update the script:
```python
import os
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
```

## Usage

### Run with sample text
```bash
python academic_bot.py
```

### Use your own text file
```bash
python academic_bot.py --input my_essay.txt
```

### Choose citation style
```bash
python academic_bot.py --input essay.txt --style apa       # APA 7th (default)
python academic_bot.py --input essay.txt --style mla       # MLA 9th
python academic_bot.py --input essay.txt --style chicago   # Chicago 17th
```

### Enable humanizer
```bash
python academic_bot.py --input essay.txt --style apa --humanize
```

## How it works

| Step | What happens |
|------|-------------|
| 1 | Claude reads your text and extracts claims that need citations |
| 2 | CrossRef API searches for real peer-reviewed papers |
| 3 | Claude picks the most credible, relevant source per claim |
| 4 | Claude formats the citation in your chosen style |
| 5 | (Optional) Claude rewrites text to sound more natural |

## Source database
Uses the **CrossRef API** — a free, open database of 150M+ scholarly works including journal articles, conference papers, and books. No scraping required.

## Tips
- **Be specific in your text** — vague claims ("some studies show...") are harder to source
- **APA is best supported** — the most common style in social sciences and psychology
- **Review citations** — always verify the found sources actually say what the claim states
- The bot inserts in-text citations immediately after each sourced sentence

## Output example

**Input:**
> Climate change is primarily driven by the burning of fossil fuels.

**Output:**
> Climate change is primarily driven by the burning of fossil fuels (Hansen et al., 2016).

**Reference:**
> Hansen, J., Sato, M., Hearty, P., Ruedy, R., Kelley, M., Masson-Delmotte, V., ... & Lo, K. (2016). Ice melt, sea level rise and superstorms. *Atmospheric Chemistry and Physics*, 16(6), 3761–3812. https://doi.org/10.5194/acp-16-3761-2016
