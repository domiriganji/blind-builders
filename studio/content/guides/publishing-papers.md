---
title: Publishing articles and papers
summary: Where to share your work quickly (preprint servers), how to choose and submit to a journal, how to make an accessible PDF from Word, LibreOffice or LaTeX, and the screen reader problems found in journal submission systems.
last_checked: 2026-10
order: 5
---

## The short version

- A **preprint server** puts your paper online in days, free, before or alongside peer review.
- A **journal** adds peer review. Check that it is real before you submit, and check its submission system early.
- The most common journal system, Open Journal Systems, had many screen reader problems reported in 2026. If a form stops you, email the editor and ask to submit another way.
- An **accessible PDF** is a tagged PDF: real headings, a title, a language, text alternatives for figures and a correct reading order.
- **LaTeX** can now produce tagged PDFs for many documents, but not every package supports it yet.
- Use [Write and publish](/write) to draft and get suggested changes one at a time, and [Research](/research) to find prior work with sources.

## Sharing quickly: preprint servers

A preprint is a full paper shared publicly before, or while, a journal reviews it. Most preprint servers are free and give your paper a permanent link.

- **arXiv** ([arxiv.org](https://arxiv.org)): physics, mathematics, computer science, statistics, quantitative biology, quantitative finance, economics, and electrical engineering. It accepts LaTeX source, and for many papers it also produces an HTML version, which is often easier to read with a screen reader than a PDF. First-time authors in some subjects need an endorsement from an existing author.
- **bioRxiv** and **medRxiv** ([biorxiv.org](https://www.biorxiv.org), [medrxiv.org](https://www.medrxiv.org)): biology, and medicine and health.
- **SSRN** ([ssrn.com](https://www.ssrn.com)): social sciences, law and economics.
- **OSF Preprints** ([osf.io/preprints](https://osf.io/preprints)): many subjects, including psychology through PsyArXiv.
- **Zenodo** ([zenodo.org](https://zenodo.org)): any research output, including data and software, with a DOI.

Before you post, check whether the journal you plan to submit to accepts papers that were shared as preprints. Most journals say so on their author instructions page.

## Choosing a journal

- Read a few papers the journal published recently. Is your work the same kind?
- For open access journals, check the [Directory of Open Access Journals](https://doaj.org).
- Use the [Think. Check. Submit.](https://thinkchecksubmit.org) checklist to avoid journals that take fees and do not really review papers.
- Find the journal's "instructions for authors" page and the editorial office's email address before you start. You may need it.

## Journal submission systems and screen readers

Thousands of open access journals run on Open Journal Systems (OJS) from the Public Knowledge Project. From April 2026, testers using screen readers such as JAWS and NVDA filed a long series of accessibility issues against version 3.5, and the audit continued into September. What they found:

- The Title and Abstract fields are rich-text editors. With JAWS and NVDA they force switching between modes, and the system can say "no title provided" even when you typed one ([issue 12601](https://github.com/pkp/pkp-lib/issues/12601)).
- A rich-text area in the settings could not be read with a screen reader ([issue 12721](https://github.com/pkp/pkp-lib/issues/12721)).

Both of these issues were still open in October 2026. Other systems, such as ScholarOne and Editorial Manager, were not checked by the research behind this guide.

What to do:

1. Before you write the cover letter, open the journal's submission form and move through it once with your screen reader.
2. In rich-text fields, type, then move away and come back to confirm your text is still there.
3. If a form blocks you, email the editorial office. Say which screen reader you use and which step does not work, and ask to send your files by email or have the editor enter the details for you. You are asking them to fix their barrier, not for a favour.
4. Keep your own copy of the title, abstract, keywords and author details in a plain text file, so you can paste them again quickly.

## Making an accessible PDF

A PDF is accessible when it is **tagged**: it carries its structure (headings, lists, tables, figures and reading order) along with the text. Whatever you write in, the same things matter:

- Use real headings, not bold text.
- Set the document title and language.
- Give every figure a text alternative that says what it shows, and put the data of each chart in a table or the text.
- Give tables a header row.
- Keep the reading order the same as the logical order.

From **Word** on Windows: use the built-in heading styles, then save as PDF with the option "Document structure tags for accessibility" turned on.

From **LibreOffice Writer**: use the heading styles, then export as PDF with the "Universal Accessibility (PDF/UA)" option turned on.

To check a PDF, two free tools are widely used: [PAC](https://pac.pdf-accessibility.org), the PDF Accessibility Checker, on Windows, and [veraPDF](https://verapdf.org), which checks against the PDF/UA standard. Like the checks on this site, they cannot tell you how the PDF sounds; ask a member to read it with their screen reader.

## LaTeX and tagged PDF

The LaTeX Project is building tagging into LaTeX itself, so ordinary documents produce tagged PDFs.

- The June 2026 LaTeX release reimplemented headings, theorems and block structures with templates, removed "fake math" from LaTeX's internals because it was "a serious tagging issue", and added new keys in `\DocumentMetadata` to set the document's language ([LaTeX News issue 43](https://raw.githubusercontent.com/latex3/latex2e/main/base/doc/ltnews43.tex)).
- The draft notes for the November 2026 release say "our major focus remains on improving the out-of-the-box accessibility of LaTeX", and report a TUG 2026 talk on making accessible presentations with ltx-talk. The draft's own section about ltx-talk still only says "to write", so check for it in the final release ([LaTeX News issue 44, draft](https://raw.githubusercontent.com/latex3/latex2e/develop/base/doc/ltnews44.tex)).

How to use it: tagging is turned on with a `\DocumentMetadata{...}` line at the very top of your file, before `\documentclass`. The exact keys have changed between releases, so copy them from the LaTeX Project's [tagging project page](https://latex3.github.io/tagging-project/) for the release you have, and check its list of which classes and packages already work with tagging. Not all of them do yet. Journal templates often use older classes, so ask the journal what it accepts.

Writing maths: NVDA 2026.1 (May 2026) has MathCAT built in for reading maths ([NVDA releases](https://github.com/nvaccess/nvda/releases)).

We could not check how well Overleaf's online editor works with screen readers. If you use it, share what works in [Ask the community](/help/ask?title=Writing%20LaTeX%20with%20a%20screen%20reader).

## You are not alone in this

In a 2026 study of 57 blind and low-vision researchers, nearly one in five could not do a literature review or evaluate visual outputs on their own ([Khan and Seo, CHI 2026](https://doi.org/10.1145/3772318.3791242)). Ask the community, use peer checks for your PDFs, and share what works.
