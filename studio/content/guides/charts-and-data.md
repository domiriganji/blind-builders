---
title: Understanding charts and data without sight
summary: Get the data behind a chart, hear it as sound, read it on a braille display, feel it as a tactile graphic, and ask the AI about charts without being misled. With sources.
last_checked: 2026-10
order: 7
---

## The short version

- **Ask for the data first.** A table of the numbers behind a chart is the most reliable way to understand it.
- **Sonification** turns values into sound, usually higher pitch for higher values, so you can hear a trend.
- **MAIDR** is a free, open tool that makes charts explorable by keyboard, sound, braille and text, and it works from Python, R and JavaScript.
- **Tactile graphics** and refreshable tactile displays let you feel the shape of a chart.
- **AI descriptions of charts can be wrong.** Give the AI the data, not just the picture, and ask it which claims come straight from the chart.

## Start with the data

A chart is a picture of numbers. If you can get the numbers, you can get everything the chart shows, and more.

- For a chart in a report or paper, look for a data table, an appendix or a supplementary data file.
- For a chart someone sent you, ask them for the spreadsheet it came from.
- For a chart you make yourself, keep the data next to it, and add a sentence that states the main finding with the key numbers.

When you describe a chart for others, say: what kind of chart it is, what is measured on each axis and in what units, the range of the values, the main trend, and any value that stands out.

## Hearing a chart: sonification

Sonification plays the data as sound. In a line chart, a rising pitch means rising values. You can hear the overall shape in a few seconds, then move through the points one at a time to hear exact values.

## MAIDR: charts you can explore by keyboard, sound, braille and text

MAIDR comes from the xability lab at the University of Illinois. It makes a chart explorable four ways: moving point by point with the keyboard, hearing it as sound, reading it on a braille display, and reading text descriptions ([MAIDR](https://github.com/xability/maidr/releases)).

- **From Python:** install py-maidr with `pip install maidr`. Add `import maidr` to a script that draws charts with matplotlib or seaborn, and the charts become explorable. It also covers Plotly, Altair and others, in scripts, Jupyter notebooks, Quarto, Shiny and Streamlit. The latest release in the research behind this guide was 1.26.0, on September 28, 2026 ([py-maidr on PyPI](https://pypi.org/project/maidr/)). Its README lists the keyboard shortcuts ([py-maidr on GitHub](https://github.com/xability/py-maidr)).
- **From R:** there is r-maidr ([r-maidr on GitHub](https://github.com/xability/r-maidr)).
- **With AI coding agents:** maidr-skill teaches Claude Code, Codex, Cursor, Copilot and Gemini CLI to make accessible MAIDR charts and check them. Install it with `npx skills add xability/maidr-skill` ([maidr-skill](https://github.com/xability/maidr-skill)).
- **Inside AI chats:** maidr-mcp, from October 2026, shows explorable charts inside ChatGPT and Claude. It had been tested only in a reference host, not yet in claude.ai or ChatGPT themselves ([maidr-mcp](https://github.com/xability/maidr-mcp)).
- **On a tactile display:** from version 4.7.0 (September 2026), MAIDR can draw charts on a DotPad tactile display.

A small example in Python:

```
import maidr
import matplotlib.pyplot as plt

plt.bar(["Monday", "Tuesday", "Wednesday"], [12, 30, 21])
plt.title("Cakes sold")
plt.show()
```

## Feeling a chart: tactile graphics and tactile displays

- **Printed tactile graphics** are raised-line versions of charts, made on swell paper, with an embosser or with a 3D printer. A 2026 study with 12 blind and low-vision people found that tactile templates helped them build a mental picture of complex chart types, such as heatmaps and violin plots, which then made questions to an AI chatbot more useful ([Touching or Chatting, VIS 2026](https://arxiv.org/abs/2607.23065)).
- **Generated tactile graphics:** researchers are building tools that turn a text prompt into a 3D-printable tactile graphic with braille, tested with blind and low-vision people on printed results ([Text-based Tactile Graphics Generation, ECCV 2026](https://ruihangao.github.io/Text2TactileGraphics/)).
- **Refreshable tactile displays** raise pins to draw a picture you can feel, and change it. In a 2026 study with 11 visually impaired people, sound cues helped them understand where things were and find their place again, and sound controls worked best when kept simple so hands could stay on the display ([EuroHaptics 2026](https://link.springer.com/chapter/10.1007/978-3-032-32230-2_22)).

## Asking the AI about a chart, without being misled

AI can describe a chart in seconds, but it can add things the chart does not show.

- An audit of 1,224 AI chart descriptions sorted each claim into three kinds: read straight from the chart, worked out from the chart, and guessed. Giving the AI structured information about the chart did more to keep its claims true to the chart than adding the image ([Chart-Supported or Model-Supplied?, 2026](https://arxiv.org/abs/2607.25021)).
- In a 2026 study, 12 blind spreadsheet users never fully trusted AI output without checking it, and checking cost them the most for charts and formatting ([Perera and others, CHI 2026](https://dl.acm.org/doi/10.1145/3772318.3790988)).
- In a 2026 study, blind and low-vision scientists asked an AI almost double the share of questions about figures and tables, and vague image descriptions made them give up ([Querying Multimodal Scientific Papers with AI, 2026](https://arxiv.org/abs/2607.18514)).

What to ask:

1. "Give me the data behind this chart as a list of values." If it cannot read exact values, it should say so.
2. "For each claim, say whether you read it directly from the chart, worked it out, or are guessing."
3. "What is on each axis, and what are the units?"
4. Then check one or two values against the source, or ask a member to.

## Tools that changed in 2026

- **JupyterLab 4.6.0** (June 2026): the terminal no longer traps keyboard focus, toolbar buttons announce whether they are pressed, and there were screen reader fixes in the launcher and notebooks ([JupyterLab changelog](https://github.com/jupyterlab/jupyterlab/blob/main/CHANGELOG.md)).
- **NVDA 2026.1** (May 2026) has MathCAT built in for reading maths ([NVDA releases](https://github.com/nvaccess/nvda/releases)).

If you find a way of reading data that works well for you, share it in [Ask the community](/help/ask?title=Reading%20charts%20and%20data).
