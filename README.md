# billizon-autopost

Posts Billizon book content to Instagram (@billizon_) on a schedule, using the
Meta Graph API. Runs entirely on GitHub Actions. Nothing needs to be running on
a laptop.

## How it works

`posts/queue.json` is a list of posts. Three times a week the workflow takes the
first entry that has not been posted yet, publishes it, then writes `posted_at`
and `media_id` back into the file and commits.

Images live in `images/` and are served from this repo's raw URLs. That is not a
convenience: Instagram will only accept an image it can fetch from a public URL,
so the repo has to be public. It doubles as an archive of everything ever posted.

## Setup

Two repository secrets, under Settings > Secrets and variables > Actions:

| Secret       | Value                                                  |
| ------------ | ------------------------------------------------------ |
| `IG_USER_ID` | `17841423795250991`                                    |
| `IG_TOKEN`   | The long lived **Page** access token                    |

Use a Page token, not a User token. User tokens expire after 60 days; Page
tokens derived from a long lived User token do not expire at all, so there is
nothing to refresh and nothing to silently break two months from now.

Never put the token in a file in this repo. It is public.

## Adding posts

1. Drop a JPEG into `images/`
2. Add an entry to `posts/queue.json`:

```json
{
  "image": "monster-atlas-strigoi.jpg",
  "caption": "Your caption here.\n\n#coloringbook #folklore",
  "posted_at": null,
  "media_id": null
}
```

Order in the file is the order they post. Leave `posted_at` and `media_id` null.

## Image rules

Instagram rejects anything that breaks these, so the script checks them before
spending an API call:

- **JPEG only.** PNG and WebP are rejected outright.
- **Aspect ratio between 4:5 and 1.91:1.** KDP covers are about 1:1.6, which is
  taller than the 4:5 floor, so they need padding onto a 4:5 canvas rather than
  cropping. Pick one canvas colour and reuse it so the grid looks deliberate.
- **8 MB maximum.**
- Caption up to 2,200 characters and 30 hashtags.

## Testing

Actions tab > Post to Instagram > Run workflow. Leave "dry run" ticked and it
validates the next entry and prints the caption without publishing. Untick it to
post for real, immediately, outside the schedule.

## Schedule

Mon, Wed, Fri at 17:10 UTC. GitHub's scheduler is best effort and can run late
when the platform is busy, sometimes by a fair margin. That is fine for this and
not worth engineering around.

## When something breaks

The run fails loudly rather than posting something wrong. Common causes:

- **Queue empty.** The job says so and exits cleanly. Top it up.
- **Token invalidated.** Page tokens do not expire, but they are revoked if the
  Facebook password changes, if the app's access is removed under Facebook
  Settings > Apps and Websites, or if permissions are withdrawn. Re-run the token
  steps and update the `IG_TOKEN` secret.
- **Image rejected.** Almost always the wrong format or aspect ratio.
