# Advanced usage

Everything behind the **Enable Advanced Mode** button: secrets and
buildups, studying a story, rewriting single paragraphs, feedback, and
fine control over what the model is sent.

← [Back to the README](../README.md) · New here? Start with [Basic usage](USAGE.md)

---

## Turning it on

Click **Enable Advanced Mode** at the top left. It changes to
**Advanced Mode: ON**, and the app shows every tab, a second bar under
the header, the right-hand column and all the settings. Your choice is
remembered in this browser.

Turn it off again and the app goes back to the simple view: only the
**Writing** tab, and every hidden setting falls back to its default (one
version, the default model and length, no playbooks, no review before
sending).

Contents:

1. [Secrets, buildups and planted facts](#1-secrets-buildups-and-planted-facts)
2. [Chapter facets](#2-chapter-facets)
3. [Rewriting a single paragraph](#3-rewriting-a-single-paragraph)
4. [Feedback](#4-feedback)
5. [Versions](#5-versions)
6. [Model and length settings](#6-model-and-length-settings)
7. [Checking a request before it is sent](#7-checking-a-request-before-it-is-sent)
8. [Studying a story](#8-studying-a-story)
9. [Shared material](#9-shared-material)
10. [Playbooks](#10-playbooks)
11. [The other tabs and buttons](#11-the-other-tabs-and-buttons)

---

## 1. Secrets, buildups and planted facts

The **second bar** under the header holds your *planted facts*: things
the model must keep carrying from a chosen point in the story onward.
Each button is a category, with a count of how many of its facts are
active where you are standing.

The built-in categories:

| Category | What the model is told |
|---|---|
| **Secrets** | A secret stays a secret, even once it has been introduced. It may pay off in a scene. |
| **Buildups** | Groundwork to lay toward what is coming — never resolved or revealed too early. |
| **Events** | Things that should happen from about this point on, without being forced. |
| **Items**, **Persons** | Things and people that exist in the story from this point. |
| **Underneath** | What is going on under the surface. |
| **Lines**, **Set pieces** | Lines of dialogue and moments you want used. Once an accepted chapter plot uses one, it is marked **✓ used**; **↺** puts it back. |

**To plant a fact:**

1. Click a category in the second bar. Its panel opens.
2. Click **＋ add a row**. The row starts at the chapter and part you
   have selected.
3. Choose the **chapter** (and, if you want, the **part**) where it
   enters the story. **–** means the start of the chapter.
4. Give it a **label** (for example *lamp* or *Mira*). Rows with the
   same label are read as one thing, nicknames included.
5. Write what the model should carry from here on.

A fact is active for every request at or after its chapter and part,
and never before. It never expires: untick **Enabled** to switch it
off, or **✕** to remove it.

**Your own category:** click **＋** at the end of the bar, give it a
**name** and **the instruction the model obeys**, and click
**Add category**.

## 2. Chapter facets

Facets describe a chapter in labelled lines that ride along with every
part of that chapter.

With a chapter selected, click **Facets…**. The seven facets are:
**Atmosphere**, **Location**, **People involved**, **Mood shifts to**,
**Underneath** (thoughts, games — shown, never announced),
**Secret** (known to the writer, never said aloud) and
**Overarching** (why here, the history). Fill in any of them and click
**Save facets**. The chapter shows how many are set and the exact lines
they become.

Stuck? **Suggest directions…** proposes whole facet sheets: take one with
**Use this direction**, or a single facet with **⇤**.

## 3. Rewriting a single paragraph

The right-hand column, the **Content bar**, rewrites one paragraph and
leaves the rest of the part alone.

1. Click a paragraph, in the text or in the list on the left.
2. Under **Replace selection**, write what to change.
3. *(Optional)* Set **how many versions** (1–5), and give each version
   its own direction.
4. Click **⚙ Generate rewrites**.
5. The versions appear under **Results from rewrite**. Click
   **Pick this** on the one you want.

Picking makes a new version of the part with only that paragraph
changed. If a rewrite changes the number of paragraphs, a warning tells
you before you pick.

The same column holds **This book's instructions** — plot, descriptions
and anything else — which are sent with every request.

## 4. Feedback

Tell the model what works and what does not; your remarks ride along
with later requests.

**Thumbs.** Each paragraph has two pairs of thumbs in the margin:
📖 for the story (what happens) and ✒️ for the writing (how it is
written). Click 👍 or 👎 and add a comment if you like; click the same
thumb again to clear it. On a 👍 you can also give it 1–3 ★ and tag what
the paragraph is good at. A 💬 shows when a comment is attached.

**Where it goes.** Story remarks ride along when plots and parts are
written. Writing remarks ride along when parts are written and
paragraphs are rewritten.

**How much of it.** In the write panel, the **feedback** line controls
each channel: tick it on or off, and set **back** to how many parts back
the remarks reach. The readout says how many of your remarks will ride.
**how many … of each group** limits the count; the best-rated and newest
go first.

**⚑ Marks.** Click ⚑ beside a paragraph, or select some words first to
mark just those. Choose plot, style or theme, say why it is good, give it
stars, and click **Mark**. Marks are what a study learns from (see
[Studying a story](#8-studying-a-story)).

## 5. Versions

With a part selected, the write panel's **versions** box (1–5) sets how
many versions one click writes. With two or more, each can get its own
direction ("darker", "no dialogue", "from her side"); leave one empty to
let the model choose.

The versions appear as candidates. **read** opens one in full, with
**‹ prev** and **next ›**; **Pick this** makes it the version that counts.
The others are kept.

## 6. Model and length settings

In the write panel:

- **model** — which model writes. This is one setting for the whole
  app: plots, parts and rewrites all use the model chosen here.
- **max tokens** — the longest a part may be (default 2000).
- **paragraphs** — how dense the part should be.

On a chapter:

- **plot size** and **each part plot** set the token budget and the
  range of beats for the chapter plot and for each part's plan. The app
  shows how many beats the budget can hold.

On the **Outline** tab, two sliders set how adventurous the model is:
**explore** for planning and suggestions, **prose** for writing.

## 7. Checking a request before it is sent

In Advanced Mode, every request opens **Before it goes to the model**
first:

- **what the model will read — in this order**: every section of the
  prompt, with its size. **expand all** shows the full text.
- Whether it **fits** the model's window.
- **story so far: the last … words ride as prose** — how much of the
  earlier text is sent word for word.
- **included material — untick to leave out of THIS request**: the
  writing rules, the summary of earlier parts, your standing
  instructions, planted facts, story feedback and writing feedback.

Click **⚡ Send** to send it, or **Cancel** to put everything back.
**skip this check until reload** sends straight away from then on.

## 8. Studying a story

Advanced Mode adds two tabs: **Make Study** and **Study**. A study reads
your chapters (or your feedback) and turns what it finds into *sheets*
that guide later writing. The **Study this story →** link in the Writing
tab takes you there.

**Make Study** has three columns:

1. **Subjects** — your chapters, your **Feedback**, and texts read by the
   extractor. Select one and click **Study…**, tick what to look at
   (style, theme, plot) and click **▶ Study it**.
2. **Questions** — your own questions.
   - **✎ Topics**: add questions with **+ Add topic** and run them with
     **▶ Run ticked**.
   - **⛏ Datamine**: **⚙ Suggest extracts** lets the model read every
     chapter and propose questions. Tick the useful ones and click
     **▶ Run checked**; **✎ Keep these as topics** saves them.
3. **Build** — tick findings, choose a new sheet or one to update, add an
   instruction if you like, and click **Build the artifact**. The result
   waits under **Waiting for you**: **Apply** writes it as a new version,
   **discard** drops it. Sheets keep proper nouns out, so they describe
   *how* you write rather than *what* happens.

The **Study** tab lists every study's findings and every sheet. Each
sheet says whether it guides **both steps**, **plot only** or
**prose only**.

## 9. Shared material

*Pools* are shared material: background, notes or extracted text that
can ride along with requests. Manage them on the **Project** tab
(**+ pool**, edit, retire). Each is either **always** sent or
**optional**, chosen per request with **choose…** on the chapter.

## 10. Playbooks

A playbook is a named set of settings. Where playbooks exist (under the
plot button, in the write panel and in the Content bar), click their
names to arm them: with several armed, one click runs the request once
per playbook, so you can compare the results side by side. **Normal**
is the control: it changes nothing.

## 11. The other tabs and buttons

| Tab | What it is for |
|---|---|
| **Project** | Your stories, shared material and housekeeping. |
| **Outline** | A card board to shape the story; the temperature sliders and **Style…** live here. |
| **Make Study** / **Study** | See [Studying a story](#8-studying-a-story). |
| **Prompts** | Every wording the app can send, with its version. |
| **Extract** | A scanner that pulls plot, style, people and more out of a manuscript. |
| **Auto** | Every model writes the whole story, so you can compare them. |
| **Bench** | One prompt straight to one model. |
| **Review** | Walk a finished run stage by stage. |
| **Loading** | What is actually on disk. |

| Button | What it does |
|---|---|
| **🕶 Anonymous** | Stops recording the text you type in the session record. |
| **🗑 Delete mode** | Shows ✕ delete buttons in the list and beside paragraphs. Deleted text is kept in a backup. |
| **Export JSON** | Downloads the whole story state, with its undo history. |

Available in both modes: **📄 Import a document** (an existing .txt or
.md becomes the next chapter, and the story continues from it),
**notes for this part** (standing notes sent every time that part is
written), **📝 Notes**, undo and redo, and **Download Story**.
