---
name: launch
description: Launch an open-source GitHub repo with Surya Distro. Use when the user wants to launch, announce or promote a repo, or study what posts work in its space. Also for writing a Show HN, Reddit, X, Bluesky, LinkedIn or Product Hunt post, finding where to submit a tool, or measuring and learning from posts they made.
---

# Launch a repo

Surya Distro studies, drafts, checks, measures and learns. It never posts, and neither do you.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/surya-distro" draft <owner/repo>
```

That runs the study and the strategy first if they're missing or stale, then drafts. Everything lands under `~/.surya-distro/<owner>/<repo>/`. A study can take a few minutes, so run it in the background and tell the person it's running.

1. **Show the study.** Read `study/study.md`. Tell the person, in a few lines, which formats beat their baseline, and which small or young accounts won and how. Say which engine labelled the posts.
2. **Show the playbook change.** If `draft` says a change is waiting, summarise `playbook.proposed.md`: each rule, its evidence and its confidence. Ask them to approve it in their own terminal with `surya-distro approve <owner/repo> playbook`. Until they do, drafts follow the approved playbook only. To show drafts from the change meanwhile, run `draft <owner/repo> --preview --force`. Those drafts can't be approved until the playbook is.
3. **Read `kit/brief.md` and the drafts.** The brief holds the facts, the playbook's rules per channel and each channel's limits.
4. **Rewrite each draft to follow the playbook.** Use only facts in the brief. Keep it plain. Say what the tool does and what it doesn't. No hype words, superlatives or em dashes.
5. **Offer to make the images and videos.** Run `create <owner/repo>` and show the person the files in `kit/media/`, with what each one follows (the rule or study finding, or default). Open the PNGs for them and say which videos exist. Run it only if they say yes. Needs Chrome and ffmpeg. If either is missing, say what to install.
6. **Ask the person for what only they know.** Every `[[fill: ...]]` marks one. That covers why they built it, the problem in their words, who it's for, a real result, the feedback they want and their name. Ask for all of them in one message. Use their words, and don't invent a motive, a number or a result.
   A video's problem line is one of those. Pass it with `create <owner/repo> --set problem="..."`, in their words.
7. **Run the checks** and fix every error:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/surya-distro" check <owner/repo>
   ```

8. **Show them the review sheet.** Run `show <owner/repo>` and give them the path to `kit/review.md`.

## The app

If they would rather see and edit it on a page, say **"open the app"** and run it in the background:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/surya-distro" app
```

It opens their browser. Every step, the study, the playbook, the drafts with live checks, the media and the results are there. It never approves or posts, and shows the approve command for them to copy into their own terminal. Give them the link it prints if the browser doesn't open.

## After they post

- They record each post: `posted <owner/repo> <draft> --url LINK`.
- `measure` reads posts at about 1 hour, 24 hours and 7 days. For X and LinkedIn, ask them for the numbers and pass them with `--likes`, `--replies`, `--reposts`, `--views` and `--followers`.
- `report <owner/repo>` shows each post beside the GitHub traffic that followed.
- `learn <owner/repo>` writes one note per finding and proposes a playbook change. Show them the diff. They approve it in their own terminal.
- `schedule` prints an hourly job for `measure`. Installing it is their choice, so give them the text.

## What you never do

- Post, submit, comment, open an issue or pull request on a list, or send an email. The person does each of those by hand.
- Run `approve` or `posted` for them. That includes `approve <repo> media:<id>`. Approval needs their own terminal, and it refuses an agent's shell.
- Pay for a listing or a scoring engine. Jev is paid and opt-in. Use it only if they set it up.
- Log in to a site, or read anything behind a login.
- Edit an approved draft without telling them. An edit makes it a draft again, and it needs a new approval.
- Copy someone else's post. The study is for formats, never words.
