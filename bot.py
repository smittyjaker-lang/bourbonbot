"""
Bourbon Tasters Discord Bot
- Contribution-tier leveling with mod-approved, capped tiers:
  Barrel Proof -> Single Barrel -> Legendary Tater Status.
- Mule rewards: in #the-mules, a thank-you that tags someone credits the mule.
- /bottleprice: auction-price links (Unicorn Auctions / Whisky Hunter).

Env: DISCORD_TOKEN (required), GUILD_ID, DB_PATH, RANKUP_CHANNEL.
"""

import os
import re
import time
import sqlite3
import urllib.parse
import discord
from discord import app_commands
from discord.ext import commands

# ---------------------------------------------------------------------------
# Config (all tunable)
# ---------------------------------------------------------------------------
TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("GUILD_ID")
DB_PATH = os.environ.get("DB_PATH", "bourbon_bot.db")
MOD_QUEUE_CHANNEL = os.environ.get("RANKUP_CHANNEL", "rank-up-queue")

TIERS = ["Barrel Proof", "Single Barrel", "Legendary Tater Status"]
TIER_THRESHOLDS = {"Barrel Proof": 750, "Single Barrel": 1500, "Legendary Tater Status": 3000}
TIER_CAPS = {"Barrel Proof": 40, "Single Barrel": 20, "Legendary Tater Status": 10}
REQUEUE_DELTA = 50

REVIEW_CHANNEL = "whisky-reviews"
DROP_CHANNELS = {"tater-drops", "chi-city-drops", "chi-burbs-drops", "online-drops"}
PRIORITY_TEXT_CHANNELS = {"general", "bourbon", "whatcha-drinking", "success"}
BOTTLE_KILL_PREFIX = "bottle-kills-only-"

BASE_MSG_PTS = 1
PRIORITY_MSG_PTS = 2
PHOTO_PTS = 5
BOTTLE_KILL_PHOTO_PTS = 15
THREAD_PTS = 10
DROP_THREAD_PTS = 25
REVIEW_MIN_LEN = 200
REVIEW_BASE_PTS = 5
REVIEW_PER_100 = 1
REVIEW_MAX_PTS = 25
EVENT_PTS = 25

MIN_MSG_LEN = 5
TEXT_COOLDOWN = 60
PHOTO_COOLDOWN = 30
REVIEW_COOLDOWN = 600

_last_text = {}
_last_photo = {}
_last_review = {}

# Mule rewards: in #the-mules a thank-you tagging someone credits the mule.
MULE_CHANNEL = "the-mules"
MULE_PTS = 50
MULE_DEDUP_HOURS = 12
THANKS_TOKENS = {
    "ty", "tysm", "thx", "thanks", "thank", "thankyou", "thanku",
    "appreciate", "appreciated", "grateful", "cheers", "salute",
}

# Unicorn search params (confirmed): "term" is the text query; state=ENDED = sold.
UNICORN_SEARCH_PARAM = "term"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS levels (
                guild_id       INTEGER NOT NULL,
                user_id        INTEGER NOT NULL,
                xp             INTEGER DEFAULT 0,
                tier           TEXT    DEFAULT '',
                last_active    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_denied_xp INTEGER DEFAULT 0,
                pending        INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rankup_queue (
                message_id   INTEGER PRIMARY KEY,
                guild_id     INTEGER NOT NULL,
                user_id      INTEGER NOT NULL,
                target_tier  TEXT    NOT NULL,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mule_awards (
                guild_id   INTEGER NOT NULL,
                from_user  INTEGER NOT NULL,
                to_user    INTEGER NOT NULL,
                ts         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    init_db()
    bot.add_view(RankupView())
    try:
        if GUILD_ID:
            g = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=g)
            await bot.tree.sync(guild=g)
        else:
            await bot.tree.sync()
    except Exception as e:
        print("Slash sync error:", e)
    print(f"Logged in as {bot.user} ({bot.user.id})")


# ===========================================================================
# Leveling helpers
# ===========================================================================
def get_level_row(gid, uid):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM levels WHERE guild_id=? AND user_id=?", (gid, uid)
        ).fetchone()
        if row is None:
            conn.execute("INSERT INTO levels (guild_id, user_id) VALUES (?, ?)", (gid, uid))
            conn.commit()
            row = conn.execute(
                "SELECT * FROM levels WHERE guild_id=? AND user_id=?", (gid, uid)
            ).fetchone()
        return row


def add_xp(gid, uid, amount):
    get_level_row(gid, uid)
    with db() as conn:
        conn.execute(
            "UPDATE levels SET xp = MAX(0, xp + ?), last_active = CURRENT_TIMESTAMP "
            "WHERE guild_id=? AND user_id=?",
            (amount, gid, uid),
        )
        conn.commit()
        return conn.execute(
            "SELECT xp FROM levels WHERE guild_id=? AND user_id=?", (gid, uid)
        ).fetchone()["xp"]


def set_pending(gid, uid, val):
    with db() as conn:
        conn.execute("UPDATE levels SET pending=? WHERE guild_id=? AND user_id=?", (val, gid, uid))
        conn.commit()


def set_denied(gid, uid, xp):
    with db() as conn:
        conn.execute(
            "UPDATE levels SET last_denied_xp=?, pending=0 WHERE guild_id=? AND user_id=?",
            (xp, gid, uid),
        )
        conn.commit()


def set_tier_db(gid, uid, tier):
    get_level_row(gid, uid)
    with db() as conn:
        conn.execute("UPDATE levels SET tier=?, pending=0 WHERE guild_id=? AND user_id=?", (tier, gid, uid))
        conn.commit()


def queue_add(message_id, gid, uid, tier):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO rankup_queue (message_id, guild_id, user_id, target_tier) VALUES (?,?,?,?)",
            (message_id, gid, uid, tier),
        )
        conn.commit()


def queue_get(message_id):
    with db() as conn:
        return conn.execute("SELECT * FROM rankup_queue WHERE message_id=?", (message_id,)).fetchone()


def queue_del(message_id):
    with db() as conn:
        conn.execute("DELETE FROM rankup_queue WHERE message_id=?", (message_id,))
        conn.commit()


def next_tier(tier):
    if tier in TIERS:
        i = TIERS.index(tier)
        return TIERS[i + 1] if i + 1 < len(TIERS) else None
    return TIERS[0] if TIERS else None


async def ensure_tier_role(guild, tier):
    role = discord.utils.get(guild.roles, name=tier)
    if role is None:
        role = await guild.create_role(name=tier, mentionable=True, reason="Contribution tier")
    return role


async def set_member_tier(guild, member, tier):
    target_role = await ensure_tier_role(guild, tier)
    to_remove = [discord.utils.get(guild.roles, name=t) for t in TIERS if t != tier]
    to_remove = [r for r in to_remove if r and r in member.roles]
    if to_remove:
        await member.remove_roles(*to_remove, reason="Tier change")
    if target_role not in member.roles:
        await member.add_roles(target_role, reason="Tier set")
    set_tier_db(guild.id, member.id, tier)


def _has_image(message) -> bool:
    for a in message.attachments:
        if (a.content_type or "").startswith("image"):
            return True
        if a.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic")):
            return True
    return False


async def maybe_nominate(guild, member, xp):
    row = get_level_row(guild.id, member.id)
    if row["pending"]:
        return
    cur = row["tier"] or ""
    nt = next_tier(cur)
    if not nt:
        return
    threshold = TIER_THRESHOLDS.get(nt)
    if threshold is None or xp < threshold:
        return
    if row["last_denied_xp"] and xp < row["last_denied_xp"] + REQUEUE_DELTA:
        return
    role = discord.utils.get(guild.roles, name=nt)
    if role and role in member.roles:
        return
    channel = discord.utils.get(guild.text_channels, name=MOD_QUEUE_CHANNEL)
    if channel is None:
        print(f"[rankup] queue channel #{MOD_QUEUE_CHANNEL} not found")
        return
    cap = TIER_CAPS.get(nt)
    cap_note = ""
    if cap is not None:
        held = len(role.members) if role else 0
        cap_note = f"\nTier cap: {held}/{cap} filled."
    cur_label = cur or "Unranked"
    embed = discord.Embed(
        title="Rank-up nomination",
        description=(
            f"{member.mention} has reached {xp} XP and is eligible for "
            f"**{nt}** (from {cur_label}).{cap_note}\n\nApprove to promote, or Deny."
        ),
        color=0x6B3F1D,
    )
    if member.display_avatar:
        embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"User ID {member.id}")
    msg = await channel.send(embed=embed, view=RankupView())
    queue_add(msg.id, guild.id, member.id, nt)
    set_pending(guild.id, member.id, 1)


async def award_message_xp(message):
    uid = message.author.id
    gid = message.guild.id
    name = getattr(message.channel, "name", "") or ""
    content = (message.content or "").strip()
    now = time.time()
    pts = 0
    if len(content) >= MIN_MSG_LEN and now - _last_text.get(uid, 0) >= TEXT_COOLDOWN:
        pts += BASE_MSG_PTS  # flat 1 in every non-special channel
        _last_text[uid] = now
    if (
        name == REVIEW_CHANNEL
        and len(content) >= REVIEW_MIN_LEN
        and now - _last_review.get(uid, 0) >= REVIEW_COOLDOWN
    ):
        pts += min(REVIEW_MAX_PTS, REVIEW_BASE_PTS + (len(content) // 100) * REVIEW_PER_100)
        _last_review[uid] = now
    if _has_image(message) and now - _last_photo.get(uid, 0) >= PHOTO_COOLDOWN:
        if name.startswith(BOTTLE_KILL_PREFIX):
            pts += BOTTLE_KILL_PHOTO_PTS
        else:
            pts += PHOTO_PTS
        _last_photo[uid] = now
    if pts > 0:
        new_xp = add_xp(gid, uid, pts)
        await maybe_nominate(message.guild, message.author, new_xp)


# ===========================================================================
# Mule rewards
# ===========================================================================
def _is_thanks(content: str) -> bool:
    low = content.lower()
    if "thank" in low or "\U0001F64F" in content:
        return True
    return any(t in THANKS_TOKENS for t in re.findall(r"[a-z']+", low))


def _recent_mule(gid, frm, to) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM mule_awards WHERE guild_id=? AND from_user=? AND to_user=? "
            "AND ts > datetime('now', ?) LIMIT 1",
            (gid, frm, to, f"-{MULE_DEDUP_HOURS} hours"),
        ).fetchone()
        return row is not None


def _record_mule(gid, frm, to):
    with db() as conn:
        conn.execute("INSERT INTO mule_awards (guild_id, from_user, to_user) VALUES (?,?,?)", (gid, frm, to))
        conn.commit()


async def maybe_award_mule(message):
    if getattr(message.channel, "name", "") != MULE_CHANNEL:
        return
    if not message.mentions or not _is_thanks(message.content or ""):
        return
    gid = message.guild.id
    thanker = message.author.id
    credited = []
    for u in message.mentions:
        if u.bot or u.id == thanker:
            continue
        if _recent_mule(gid, thanker, u.id):
            continue
        _record_mule(gid, thanker, u.id)
        new_xp = add_xp(gid, u.id, MULE_PTS)
        await maybe_nominate(message.guild, u, new_xp)
        credited.append(u.mention)
    if credited:
        await message.channel.send(f"\U0001FACF +{MULE_PTS} XP to {', '.join(credited)} for the mule!")


@bot.event
async def on_message(message):
    if message.author.bot or message.guild is None:
        return
    try:
        await award_message_xp(message)
        await maybe_award_mule(message)
    except Exception as e:
        print("XP award error:", repr(e))


@bot.event
async def on_thread_create(thread):
    if thread.guild is None or not thread.owner_id:
        return
    member = thread.guild.get_member(thread.owner_id)
    if member is None or member.bot:
        return
    parent_name = thread.parent.name if thread.parent else ""
    pts = DROP_THREAD_PTS if parent_name in DROP_CHANNELS else THREAD_PTS
    try:
        new_xp = add_xp(thread.guild.id, member.id, pts)
        await maybe_nominate(thread.guild, member, new_xp)
    except Exception as e:
        print("Thread XP error:", repr(e))


class RankupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="rankup_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_rankup(interaction, approve=True)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="rankup_deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_rankup(interaction, approve=False)


async def handle_rankup(interaction: discord.Interaction, approve: bool):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("Only moderators (Manage Roles) can decide nominations.", ephemeral=True)
        return
    rec = queue_get(interaction.message.id)
    if not rec:
        await interaction.response.send_message("This nomination is no longer active.", ephemeral=True)
        return
    guild = interaction.guild
    target = rec["target_tier"]
    member = guild.get_member(rec["user_id"])
    if member is None:
        queue_del(interaction.message.id)
        set_pending(guild.id, rec["user_id"], 0)
        await interaction.response.edit_message(content="Member is no longer in the server.", embed=None, view=None)
        return
    if approve:
        cap = TIER_CAPS.get(target)
        if cap is not None:
            role = discord.utils.get(guild.roles, name=target)
            held = len(role.members) if role else 0
            if held >= cap:
                await interaction.response.send_message(
                    f"{target} is at its cap ({cap}). Free a slot or raise the cap first.", ephemeral=True
                )
                return
        await set_member_tier(guild, member, target)
        queue_del(interaction.message.id)
        await interaction.response.edit_message(
            content=f"Approved: {member.display_name} promoted to {target} by {interaction.user.display_name}.",
            embed=None, view=None,
        )
    else:
        row = get_level_row(guild.id, member.id)
        set_denied(guild.id, member.id, row["xp"])
        queue_del(interaction.message.id)
        await interaction.response.edit_message(
            content=f"Denied: nomination for {member.display_name} to {target} by {interaction.user.display_name}.",
            embed=None, view=None,
        )


@bot.tree.command(description="Show your contribution rank and XP.")
@app_commands.describe(user="(optional) whose rank to view")
async def rank(interaction: discord.Interaction, user: discord.Member = None):
    member = user or interaction.user
    row = get_level_row(interaction.guild_id, member.id)
    xp = row["xp"]
    tier = row["tier"] or ""
    tier_display = tier or "Unranked"
    nt = next_tier(tier)
    if nt and TIER_THRESHOLDS.get(nt) is not None:
        need = TIER_THRESHOLDS[nt]
        prog = f"{xp} / {need} XP toward {nt}"
        if xp >= need:
            prog += " - eligible, awaiting mod approval"
    else:
        prog = "Top tier reached."
    embed = discord.Embed(title=f"{member.display_name} - {tier_display}", description=f"{xp} XP\n{prog}", color=0x6B3F1D)
    if member.display_avatar:
        embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(description="Top contributors by XP.")
async def leaderboard(interaction: discord.Interaction):
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id, xp, tier FROM levels WHERE guild_id=? ORDER BY xp DESC LIMIT 30",
            (interaction.guild_id,),
        ).fetchall()
    if not rows:
        await interaction.response.send_message("No XP logged yet.", ephemeral=True)
        return
    lines = []
    for i, r in enumerate(rows, 1):
        m = interaction.guild.get_member(r["user_id"])
        nm = m.display_name if m else f"User {r['user_id']}"
        lines.append(f"{i}. {nm} - {r['xp']} XP ({r['tier'] or 'Unranked'})")
    embed = discord.Embed(title="Contribution Leaderboard", description="\n".join(lines), color=0x6B3F1D)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(description="(Mod) Credit a member for attending an event.")
@app_commands.describe(user="Member who attended", points="XP to award (default event value)")
@app_commands.checks.has_permissions(manage_roles=True)
async def addevent(interaction: discord.Interaction, user: discord.Member, points: int = EVENT_PTS):
    new_xp = add_xp(interaction.guild_id, user.id, points)
    await maybe_nominate(interaction.guild, user, new_xp)
    await interaction.response.send_message(f"Gave {user.display_name} {points} XP. Now at {new_xp} XP.", ephemeral=True)


@bot.tree.command(description="(Mod) Adjust a member's XP (negative to subtract).")
@app_commands.describe(user="Member", amount="XP to add or subtract")
@app_commands.checks.has_permissions(manage_roles=True)
async def addxp(interaction: discord.Interaction, user: discord.Member, amount: int):
    new_xp = add_xp(interaction.guild_id, user.id, amount)
    await maybe_nominate(interaction.guild, user, new_xp)
    await interaction.response.send_message(f"Adjusted {user.display_name} by {amount}. Now at {new_xp} XP.", ephemeral=True)


async def tier_autocomplete(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=t, value=t) for t in TIERS if current.lower() in t.lower()][:25]


@bot.tree.command(description="(Mod) Set a member's tier directly.")
@app_commands.describe(user="Member", tier="Tier to assign")
@app_commands.autocomplete(tier=tier_autocomplete)
@app_commands.checks.has_permissions(manage_roles=True)
async def setrank(interaction: discord.Interaction, user: discord.Member, tier: str):
    if tier not in TIERS:
        await interaction.response.send_message(f"Unknown tier. Options: {', '.join(TIERS)}", ephemeral=True)
        return
    await set_member_tier(interaction.guild, user, tier)
    await interaction.response.send_message(f"Set {user.display_name} to {tier}.", ephemeral=True)


async def remove_all_tier_roles(guild, member):
    roles = [discord.utils.get(guild.roles, name=t) for t in TIERS]
    roles = [r for r in roles if r and r in member.roles]
    if roles:
        await member.remove_roles(*roles, reason="Demotion")
    set_tier_db(guild.id, member.id, "")


def current_tier_of(guild, member):
    held = [t for t in TIERS if discord.utils.get(guild.roles, name=t) in member.roles]
    if not held:
        return None
    return max(held, key=lambda t: TIERS.index(t))


@bot.tree.command(description="(Mod) Demote a member one tier (manual removal).")
@app_commands.describe(user="Member to demote")
@app_commands.checks.has_permissions(manage_roles=True)
async def demote(interaction: discord.Interaction, user: discord.Member):
    guild = interaction.guild
    cur = current_tier_of(guild, user)
    if cur is None:
        await interaction.response.send_message(f"{user.display_name} holds no tier role.", ephemeral=True)
        return
    i = TIERS.index(cur)
    if i == 0:
        await remove_all_tier_roles(guild, user)
        await interaction.response.send_message(f"Removed {cur} from {user.display_name} - now unranked.", ephemeral=True)
    else:
        new = TIERS[i - 1]
        await set_member_tier(guild, user, new)
        await interaction.response.send_message(f"Demoted {user.display_name} from {cur} to {new}.", ephemeral=True)


@bot.tree.command(description="(Mod) Flag a member for demotion review.")
@app_commands.describe(user="Member to flag", reason="Why they're flagged")
@app_commands.checks.has_permissions(manage_roles=True)
async def flag(interaction: discord.Interaction, user: discord.Member, reason: str = "No reason given"):
    guild = interaction.guild
    cur = current_tier_of(guild, user) or "Unranked"
    desc = f"{user.mention} ({cur})\nReason: {reason}\n\nA mod can run /demote to remove a tier, or leave them be."
    embed = discord.Embed(title="Flagged for demotion review", description=desc, color=0xB00020)
    embed.set_footer(text=f"Flagged by {interaction.user.display_name}")
    channel = discord.utils.get(guild.text_channels, name=MOD_QUEUE_CHANNEL)
    if channel is not None:
        await channel.send(embed=embed)
        await interaction.response.send_message(f"Flagged {user.display_name} in #{MOD_QUEUE_CHANNEL}.", ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ===========================================================================
# Bottle price lookup
# ===========================================================================
def bottle_links_view(name: str) -> discord.ui.View:
    q = urllib.parse.quote_plus(name)
    base = f"https://www.unicornauctions.com/search?{UNICORN_SEARCH_PARAM}={q}"
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Unicorn - Sold", emoji="\U0001F984", style=discord.ButtonStyle.link, url=f"{base}&state=ENDED&sortBy=number_asc"))
    view.add_item(discord.ui.Button(label="Unicorn - Live", emoji="\U0001F984", style=discord.ButtonStyle.link, url=base))
    return view


@bot.tree.command(description="Look up auction prices for a bottle (bourbon, scotch, rye, etc.).")
@app_commands.describe(bottle="Bottle name, e.g. Pappy Van Winkle 15 or Lagavulin 16")
async def bottleprice(interaction: discord.Interaction, bottle: str):
    desc = (
        "Tap below for this bottle on Unicorn Auctions (sold + live lots) and realized "
        "prices on Whisky Hunter.\n\nAuction results are hammer prices; Unicorn adds a "
        "15% buyer's premium plus tax and shipping."
    )
    embed = discord.Embed(title=f"Auction prices: {bottle}", description=desc, color=0x6B3F1D)
    embed.set_footer(text="Bourbon Tasters - bottle price links")
    await interaction.response.send_message(embed=embed, view=bottle_links_view(bottle))


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set the DISCORD_TOKEN environment variable.")
    bot.run(TOKEN)
