# Surya Distro

Launch an open-source repo from evidence. Distro studies which posts work in your repo's space right now and turns that into a playbook. It drafts each post to follow it, makes the images and videos in the formats that won, then measures what you post and learns from it. It never posts anything.

- **Study first.** It reads public posts from Hacker News, Bluesky, GitHub and, with keys, Reddit and Product Hunt. Each post is scored against what its author's size and account age predict, so small accounts that won stand out.
- **A playbook with receipts.** Every rule names the posts behind it, the numbers, the sample size and a confidence. You approve each change from your own terminal.
- **Drafts that follow it.** One draft each for Show HN, the subreddits that fit, X, Bluesky, LinkedIn, Product Hunt and newsletter emails. `check` holds them to the channel's limits and to the playbook.
- **Images and video.** `create` renders social cards, Product Hunt images and 15 to 30 second explainer videos from your README, in the media formats the study found beat their baseline. It copies shapes, never anyone's words or media.
- **Measure and learn.** It reads each post at about 1 hour, 24 hours and 7 days, and joins it to your GitHub views, clones and stars. Each finding becomes one note.
- **You decide.** Drafts and playbook changes are approved only in your own terminal, and you post by hand.

You need Python 3.9 or newer and the [GitHub CLI](https://cli.github.com), signed in. Nothing else. [Ollama](https://ollama.com) is optional and makes the study sharper.

## Install

**Claude Code.** In Claude Code:

```
/plugin marketplace add KrishantM/surya-distro
/plugin install surya-distro@surya-distro
```

Then say **"launch owner/repo"**. Claude runs the study, shows you the playbook, drafts the posts, asks you for the parts only you know, and shows you the review sheet.

**From a terminal.**

```bash
pip install git+https://github.com/KrishantM/surya-distro
```

Or clone it and run `python3 scripts/surya-distro`.

## The flow

```
study → strategy → draft → create → check → approve → post (by hand) → posted → measure → learn
```

```bash
surya-distro draft owner/repo
```

`draft` runs the study and the strategy first when they're missing or more than a week old. Then it drafts.

| Step | Command | What happens |
|---|---|---|
| Study | `study owner/repo` | Reads public posts in the repo's space and scores each one. Writes `study.md` |
| Strategy | `strategy owner/repo` | Turns the study and your own results into a playbook change, shown as a diff |
| Approve the playbook | `approve owner/repo playbook` | Shows the diff and asks you to type `playbook`. Only works in your own terminal |
| Draft | `draft owner/repo` | One draft per channel, following the approved playbook. `--preview` drafts from a change you haven't approved |
| Create | `create owner/repo` | Renders images and videos into `kit/media/`, and lists them on each draft. See Create below |
| Fill in | Edit `drafts/*.md` | Replace each `[[fill: ...]]` with your own words |
| Check | `check owner/repo` | Channel limits, placeholders, the playbook's rules, and the media files |
| Approve a draft | `approve owner/repo hn` | Shows the draft and asks you to type its name |
| Approve media | `approve owner/repo media:x-image` | Same, for one image or video. Look at the file first |
| Post | By hand | Each draft says where |
| Record | `posted owner/repo hn --url LINK` | Starts measuring it |
| Measure | `measure` | Reads each post at its 1 hour, 24 hour and 7 day marks, and stores GitHub traffic |
| Learn | `learn owner/repo` | One note per finding, and a playbook change for you to approve |

`show owner/repo` prints where the repo is in the flow. `report owner/repo` puts each post beside the traffic that followed. `schedule` prints an hourly job for `measure`, which you install yourself.

## Study

| Source | Access | What it reads |
|---|---|---|
| Hacker News | Open: the Algolia search API and the official Firebase API | Stories, points, comments, the author's karma and account age |
| Bluesky | Open: the AppView API, no login | Posts, likes, reposts, replies, quotes, followers and account age |
| GitHub | `gh`, signed in as you | New repos in the same space, stars, owner followers, and the README of the fastest growing |
| Reddit | Needs an app key. It returns 403 without one | Set `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` for an app-only read token |
| Product Hunt | Needs a developer token | Set `PRODUCT_HUNT_TOKEN` |
| X, LinkedIn | No free read API | Paste posts and their numbers into `study/manual.csv` |

It never logs in as you and never reads behind a login. Requests carry a plain User-Agent with no email address, and each host gets a gap between calls.

The queries come from the repo: its kind, such as a Claude Code plugin, and the noun its README uses most. Edit `study/config.json` or pass `--query` to change them.

**How a post is scored.** For each source, it fits engagement against the author's size and account age. A post's score is how far it lands above that, in standard deviations. A score of +1 beats about five posts in six from authors of the same size and age. Small or young accounts with high scores get their own table. Comments per like is reported as the discussion signal.

**What it tags.** The title's shape, how the post opens, its length, media and where the link sits. Also what kind of post it is, and the day and time. It copies formats, never anyone's words.

## Scoring engines

A scoring engine labels each post: whether it's about the space, what kind of post it is, and how it opens. The scores themselves are arithmetic.

| Engine | Cost | How to pick it |
|---|---|---|
| Ollama, a local model | Free | The default when Ollama is running. `SURYA_DISTRO_OLLAMA_MODEL` picks the model |
| Heuristic, rules only | Free | The default without Ollama. Always works, and is the weakest |
| Jev, TypeSafe's decision model | Paid, no free tier | `--engine jev` with `TYPESAFE_API_KEY` set. Opt-in |
| Surya Unite | Your own plans | `--engine unite`. Its chore role tries a local model, then Claude or Codex |

Every study and every rule says which engine labelled it. A post an engine doesn't answer for gets the heuristic's label, marked as a fallback. The Jev key is read from the environment and sent only in its Authorization header. It is never logged or stored.

### Adding Jev

Distro works fully without Jev. The local model is the default, and the rules-only engine needs nothing at all. If you have a key for [Jev](https://typesafe.ai), TypeSafe's decision model, it can improve a study in three ways:

- **Bigger studies.** Labelling is the slow step. A local model took about 8 minutes for 387 posts. TypeSafe reports replies in under half a second, so a study can read thousands of posts. More posts means more rules reach medium or high confidence.
- **Calibrated relevance.** Jev returns a probability with each label, trained to match how often it's right. Distro uses the relevance probability to decide which posts belong to your space, so off-topic posts stay out of the baseline.
- **No broken answers.** Jev can only answer in the shape it was asked for. Fewer posts fall back to the heuristic because a model's reply couldn't be read.

To use it, set the key and pick the engine:

```bash
export TYPESAFE_API_KEY=your-key
```

```bash
surya-distro --engine jev study owner/repo
```

`SURYA_DISTRO_ENGINE=jev` makes it the default. Distro never picks Jev on its own, so a key in your environment never costs you by surprise. Jev is in early access and billed per token, with no free tier. The benefits above are TypeSafe's published claims. Distro's Jev engine follows TypeSafe's API reference but hasn't been run against the live API.

## The playbook

The strategy compares the formats that beat their baseline with your product's facts and its likely audience. It writes rules per channel: format, opening, length, media, link placement and timing, plus what to avoid. Formats are compared among launch posts when there are enough of them.

Each rule shows its evidence. That means the posts behind it, the mean score with and without the format, the sample size and a confidence. High needs 15 or more posts and a clear gap. Medium needs 6 or more.

- A change is shown as a diff. It is used only after you approve it in your own terminal.
- Each approval goes in the decisions log, with the before, the after, the evidence and the date.
- Rules that stop being supported are marked superseded and kept.
- Rules that rest only on your own launches are marked early, because one launch is a sample of one.

`check` enforces the rules it can count, such as title shape, length and link placement, as errors at medium or high confidence. Rules about how a post opens are warnings, because they rest on a label. A draft written from a change you haven't approved is held to that change, so you can fix it first.

## Create

`create` makes the images and videos for a launch. The rule is to replicate a trend's format and never its content. It reads only shapes: whether a format won, its aspect and its length.

```bash
surya-distro create owner/repo
```

**What it makes.**

| Channel | Files |
|---|---|
| Link previews | `og-card`, 1200x630 |
| README | `readme-hero`, 1280x640 |
| X | `x-image` 1600x900, `x-video-16x9`, `x-video-1x1` |
| Bluesky | `bluesky-image` 1600x900, `bluesky-video-16x9` |
| LinkedIn | `linkedin-image` 1200x1200, `linkedin-video-1x1`, `linkedin-video-9x16` |
| Product Hunt | `producthunt-thumb` 240x240, `producthunt-gallery-1` to `-3` at 1270x760 |

**Which ones, and why.** The study now reports which media formats beat their baseline on each channel: video, GIF, image, text only, and a screenshot in the README. Each row has its sample size and confidence, in `study.md`. For each channel `create` follows, in order, a rule in the approved playbook, then a study finding, then a default. If X's rule says video did best, X gets the 16:9 and 1:1 videos. If it says text alone did best, X gets none, and `create` says so. Every file records what it followed in `media.json`. `--only id,id` picks files by hand.

**The videos.** Each is 15 to 30 seconds. Scenes are the problem, what it does, up to three features, the install command typed in a terminal, and a closing card with the repo URL. A README screenshot or GIF gets its own scene with a slow zoom. The captions are burned in, because most social video is watched muted. Facts come only from the README, the description and the release.

**What only you know.** The problem a tool solves is not in most READMEs. Where a fact is missing, the file shows a visible `[[fill: ...]]` marker, and `check` and `approve` refuse it. Give the words with `--set`:

```bash
surya-distro create owner/repo --set problem="your own words"
```

Words you give are kept for the next run. `--set` takes `problem`, `tagline`, `name` and `install`.

**Style.** The repo picks the colours from its name, and the fonts come from the system stack, so nothing loads from the network. `--style FILE` takes a JSON object with any of `bg`, `panel`, `fg`, `muted`, `accent`, `accent_fg`, `line`, `font_display`, `font_sans` and `font_mono`.

**Voice.** Off by default. `--voice` reads the caption text with macOS `say`, or with the command in `SURYA_DISTRO_TTS`. That command gets the text on stdin and writes the file named `{out}`. Each scene lasts as long as its line.

**What it needs.** Chrome, Chromium, Edge or Brave, and ffmpeg. Set `SURYA_DISTRO_CHROME` or `SURYA_DISTRO_FFMPEG` if they are not found. With either missing, `create` says what to install and still writes the HTML, which you can open in a browser. Distro drives the browser itself over a pipe, so there is still nothing to pip install.

**Approval.** Each file is a draft until you approve it in your own terminal with `approve owner/repo media:<id>`. A draft lists its media, and its approval covers each file's hash. Render a file again and it comes out different, and both approvals are gone. `check` refuses a file that still shows a marker, is the wrong size for its channel, or is over the channel's video limit: X 140 seconds, Bluesky 3 minutes, LinkedIn 10 minutes. The limits and their sources are in `surya_distro/data/media_limits.json`. `media owner/repo` lists the files.

## Measure and learn

`measure` reads each post at about 1 hour, 24 hours and 7 days where the platform allows it. Hacker News and Bluesky are open. Reddit and Product Hunt need their keys. For X and LinkedIn, type the numbers in:

```bash
surya-distro measure owner/repo x --likes 30 --replies 4 --reposts 3 --views 1200 --followers 800
```

`learn` scores each post against the study's baseline for an account its size. It joins the GitHub views, clones, stars and referrals from the week after. Each finding becomes one Markdown note, with its evidence, sample size, confidence, date and status in the frontmatter.

## Memory and your harness

Distro does the work, but the advanced steps compound only when what it learns outlives the session. Each launch's findings should shape the next launch's playbook.

- **A memory store.** Point Distro at one and the findings go there, in a `surya-distro/` folder with one index note. [Surya Memory](https://github.com/KrishantM/surya-memory) is found on its own, as is Claude Code's memory directory. Pass `--memory DIR` or set `SURYA_DISTRO_MEMORY` for any other. With no store it keeps them in `~/.surya-distro/learnings/`. If the store has a `MEMORY.md` index, Distro adds one line pointing at its index note, once, so your agents see what worked.
- **Approvals.** Distro's own approvals happen in your terminal. [Surya Gates](https://github.com/KrishantM/surya-gates) adds approval gates to the agent around it, so nothing that publishes slips through.
- **Models.** Surya Unite, part of the paid Surya app, routes the labelling to a local model, Claude or Codex.
- **Agents.** The Claude Code plugin is the main way in. Codex or any agent with a shell can run the same commands.

A visual dashboard is part of Surya Unite.

## Where it checks

`draft` and `targets` check these places live, every run.

| Place | Fits | Live check |
|---|---|---|
| Claude plugin directory | Claude Code plugins | The submission page loads |
| Official MCP Registry | MCP servers | Searches the registry API for your repo. If it's missing, checks for the PyPI or npm package the registry needs |
| Awesome Claude Code (two lists) | Claude Code tools | Repo active and not archived, your repo not already listed, and the list's age and star rules |
| Awesome Claude Plugins | Claude Code plugins | Same as above |
| Awesome MCP Servers | MCP servers | Same as above |
| mcpservers.org, MCP.so, Glama | MCP servers | The submission page loads |
| Changelog News | Open source | The submission page loads |
| PyCoder's Weekly | Python | The submission page loads |
| Console | Developer tools | The page still lists the address to email |

Each place gets a status: open, already listed, not yet, needs work first, quiet, archived, gone, or check by hand. The list lives in `surya_distro/data/targets.json`.

## Files

```
~/.surya-distro/<owner>/<repo>/
    study/study.md          outliers, and which formats beat their baseline
    study/study.json        every post read, with its numbers, tags, labels and score
    study/config.json       the queries and the description of the space
    study/manual.csv        X and LinkedIn posts you paste in
    playbook.md             the approved playbook (and playbook.proposed.md, a change waiting for you)
    kit/review.md           everything on one page: readiness, playbook state, drafts, places to submit
    kit/brief.md            facts, the playbook and channel rules, for whoever rewrites the drafts
    kit/drafts/*.md         one file per draft
    kit/media/*.png|mp4|html  the images and videos, and the HTML each is rendered from
    kit/media/media.json    every asset: channel, size, what it follows, status and hash
    posts.json              your posts, read at 1 hour, 24 hours and 7 days
    traffic.json            GitHub traffic, kept past GitHub's 14 days
```

Set `SURYA_DISTRO_HOME` or pass `--home` to keep it somewhere else.

## What has been run

| Part | How it was checked |
|---|---|
| Study, strategy, drafts, checks, approvals, measure, learn, create | 169 unit tests. Every network call goes to a fake, and the tests fail if any request reaches the network |
| Never posts | A test holds every request with a body to five read or scoring endpoints, and bans publishing endpoints from the code |
| Live study | A real run for one public repo on 12 November 2026. It read 387 public posts: 202 from Hacker News, 125 from Bluesky and 60 GitHub repos. A local model labelled them through Ollama in about 8 minutes. The playbook was written from it and the repo's drafts were redrafted to follow it |
| Create | A real run for one public repo on 17 November 2026, from a copy of its study and playbook. It rendered 9 images and 5 videos with Chrome and ffmpeg on a Mac, then I looked at the images and at frames from each video. The videos came out H.264, yuv420p, 28.6 seconds. One video was also made with `say` as the voice and a README-style screenshot scene. The tests use a fake browser that speaks the pipe protocol, and one real render of a PNG and a 2 second video when Chrome and ffmpeg are there |
| Create, not run | Linux and Windows browsers. A voice from `SURYA_DISTRO_TTS` other than `say`. A README screenshot downloaded from GitHub, since the repo used had none. The video limits for X and LinkedIn, which could not be checked on their help pages |
| Jev | Built from TypeSafe's published API reference. Not run against the live API, because it needs a paid key |
| Reddit and Product Hunt | Built from their API docs. Not run live, because both need keys |
| Install | `tests/run_installed.sh` installs into a fresh virtualenv and runs the tests against it, on Python 3.9 and 3.14. It needs the network to fetch setuptools, so for v0.3 the same tests were run against a copy holding only the files the package data lists |
| Hourly schedule | Printed only. Not installed or run |

```bash
python3 -m unittest discover -s tests -t .
```

```bash
bash tests/run_installed.sh
```

## Limits

- A study is only as good as its sample. Small samples give low-confidence rules, and the playbook says so.
- Karma and follower counts are today's, not the day of the post.
- Rules about how a post opens rest on labels, which a model can get wrong. `check` treats them as warnings.
- First drafts are templates filled from the README. In Claude Code, the skill rewrites them with you.
- Which media formats win rests on labels and small samples. The study says how many posts and how sure it is.
- Videos are made frame by frame, so a 30 second 1080p file takes about a minute.
- Built and tested on macOS. Linux should work. Windows is untested.

Every version keeps one rule: nothing is posted without a person's yes.

MIT licensed.
