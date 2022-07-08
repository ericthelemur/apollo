import asyncio
import logging
from datetime import datetime

import discord
from discord import AllowedMentions
from discord.ext import commands
from discord.ext.commands import Bot, Context, MissingPermissions
from humanize import precisedelta
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy_utils import ScalarListException

import utils.utils
from config import CONFIG
from models import Announcement, db_session
from utils import (
    DateTimeConverter,
    get_database_user,
    get_name_string,
    is_compsoc_exec_in_guild,
    user_is_irc_bot,
)
from utils.announce_utils import confirmation, generate_announcement


async def get_webhook(channel):
    """Finds announcement webhook, or creates it necessary"""
    try:
        # Find webhook
        webhooks = await channel.webhooks()
        webhook = next((w for w in webhooks if w.name == "Apollo Announcements"), None)
        if webhook is None:  # Create if not existing
            webhook = await channel.create_webhook(name="Apollo Announcements")
        return webhook
    except MissingPermissions:
        return None


class Announcements(commands.Cog):
    def __init__(self, bot: Bot):
        self.bot = bot
        self.bot.loop.create_task(announcement_check(self.bot))

    @commands.group()
    @commands.check(is_compsoc_exec_in_guild)
    async def announcement(self, ctx: Context):
        """
        Manage scheduled announcements
        """
        if not ctx.invoked_subcommand:
            await ctx.send("Subcommand not found.")

    @announcement.command()
    async def add(
        self,
        ctx: Context,
        channel: discord.TextChannel,
        trigger_time: DateTimeConverter,
        *,
        announcement_content: str,
    ):
        """
        Add an announcement for a scheduled time
        Ensure time is in quotation marks if multiple words, the announcement will the rest of discord message.
        """
        # Function very similar to reminders

        now = datetime.now()
        if not trigger_time:
            return await ctx.send("Incorrect time format, please see help text.")
        if trigger_time < now:
            return await ctx.send("That time is in the past.")

        # Preview render of announcement. If menu's input confirms, continue
        result = await self.preview_announcement(ctx, announcement_content, False)
        if not result:
            return

        # The time is valid and not in the past, add the announcement
        await add_announcement(ctx, channel, trigger_time, announcement_content)

    @announcement.command()
    async def preview(self, ctx: Context, *, announcement_content: str):
        """
        Preview the formatting of an announcement body
        """
        await self.preview_announcement(ctx, announcement_content, True)

    @announcement.command()
    async def list(self, ctx: Context):
        """
        List all upcoming announcements
        """
        # Find all upcoming announcements
        announcements = (
            db_session.query(Announcement)
            .filter(
                Announcement.trigger_at >= datetime.now(),
                Announcement.triggered == False,
            )
            .all()
        )

        msg_text = ["**Pending Announcements:**"]
        for a in announcements:
            id = a.id
            # Get author mention
            if a.irc_name:
                author_name = a.irc_name
            else:
                author_name = self.bot.get_user(a.user.user_uid).mention
            time = a.trigger_at
            loc = a.playback_channel_id
            preview = a.announcement_content.split("\n")[0]

            # Construct message
            msg_text.append(
                f"**{id}: in <#{loc}> <t:{int(time.timestamp())}:R> by {author_name}**\n\t{preview}\n"
            )

        # Send messages
        for text in utils.utils.split_into_messages(msg_text):
            await ctx.send(text, allowed_mentions=AllowedMentions.none())

    @announcement.command()
    async def cancel(self, ctx: Context, announcement_id: int):
        """
        Cancel an upcoming announcement.
        The announcement id can be found through `!announcement list`.
        """
        # Find result
        result = (
            db_session.query(Announcement)
            .where(Announcement.id == announcement_id)
            .first()
        )
        # Attempt to delete
        if result:
            db_session.delete(result)
            db_session.commit()
            await ctx.send("Announcement Deleted")
        else:
            await ctx.send("Announcement does not exist")

    @announcement.command()
    async def check(self, ctx: Context, announcement_id: int):
        """
        Check the raw source and preview of an upcoming announcement.
        The announcement id can be found through `!announcement list`.
        """
        # Find result
        result = (
            db_session.query(Announcement)
            .where(Announcement.id == announcement_id)
            .first()
        )
        # Post source and Render preview
        await ctx.send(f"**Message Source:**```\n{result.announcement_content}```")
        await self.preview_announcement(ctx, result.announcement_content, True, False)

    @announcement.command()
    async def mention(self, ctx: Context, announcement_id: int, *roles: discord.Role):
        """
        Add a role mention to the end of the messsage.
        Use this command to avoid pinging roles when writing the message. Roles can be specified by name or id.
        """
        # Find result
        announcement = (
            db_session.query(Announcement)
            .where(Announcement.id == announcement_id)
            .first()
        )
        # Add pings to message
        announcement.announcement_content += "\n" + " ".join([r.mention for r in roles])
        db_session.commit()

        role_names = ", ".join(r.name for r in roles)
        await ctx.send(
            f"Pings added for {role_names} to announcement {announcement_id}."
        )

    async def preview_announcement(
        self, ctx, announcement_content: str, preview: bool = True, menu: bool = True
    ):
        """Posts preview to command channel"""
        channel = ctx.channel
        webhook = await get_webhook(channel)

        messages = [await channel.send("**Announcement Preview:**")]
        author = ctx.author if CONFIG.ANNOUNCEMENT_IMPERSONATE else self.bot.user
        messages += await generate_announcement(
            channel, announcement_content, webhook, author.name, author.avatar_url
        )
        messages.append(await channel.send("**End of Announcement Preview**"))
        if menu:
            return await preview_edit_menu(ctx, messages, announcement_content, preview)


async def announcement_check(bot):
    """Checks for any announcements that need to be posted and haven't"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        # Find announcements that need posting
        now = datetime.now()
        announcements = (
            db_session.query(Announcement)
            .filter(Announcement.trigger_at <= now, Announcement.triggered == False)
            .all()
        )

        for a in announcements:
            channel = bot.get_channel(a.playback_channel_id)
            webhook = await get_webhook(channel)

            # Find author info
            name, avatar = None, None
            if a.irc_name:
                name = a.irc_name
            else:
                author = (
                    bot.get_user(a.user.user_uid)
                    if CONFIG.ANNOUNCEMENT_IMPERSONATE
                    else bot.user
                )
                name = author.name
                avatar = author.avatar_url

            message = a.announcement_content
            a.triggered = True
            db_session.commit()

            # Post message
            await generate_announcement(
                channel, message, webhook, name, avatar, AllowedMentions.all()
            )

        await asyncio.sleep(CONFIG.ANNOUNCEMENT_SEARCH_INTERVAL)


async def preview_edit_menu(ctx, messages, announcement_content, preview):
    """Menu to post, edit or cancel preview"""

    async def interact(msg, reaction):
        """Function called on user react to menu"""
        await msg.delete()
        # If edit or delete, remove old messages
        if str(reaction) in {"❌", "✏️"}:
            for ann_msg in messages:
                await ann_msg.delete()

            if str(reaction) == "❌":
                await ctx.send("Announcement cancelled")
            if str(reaction) == "✏️":
                edit_msg: discord.Message = await ctx.fetch_message(ctx.message.id)
                await ctx.bot.process_commands(edit_msg)
            return False

        if preview:
            await ctx.send(
                f"Preview complete. Send this message with\n`!announcement add #announcements 10s \n{announcement_content}`"
            )
        for ann_msg in messages:
            await ann_msg.delete()
        return True

    async def timeout(msg):
        """Function called if timeout (currently 5 mins) if reached"""
        await msg.delete()
        await ctx.send(
            f"**Timeout.** Restart posting with: `!announcement preview {announcement_content}`"
        )
        for ann_msg in messages:
            await ann_msg.delete()
        return False

    return await confirmation(
        ctx,
        f"Edit Preview",
        f"  ✅ to {'finalize' if preview else 'schedule announcement'}\n  ✏️ to edit (make changes in source first)\n  ❌ to cancel",
        ["✅", "✏️", "❌"],
        interact,
        timeout,
        300,
    )


async def add_announcement(ctx, channel, trigger_time, announcement_content):
    display_name = get_name_string(ctx.message)
    # Set the id to a random value if the author was the bridge bot, since we won't be using it anyway
    # if ctx.message.clean_content.startswith("**<"): <---- FOR TESTING
    if user_is_irc_bot(ctx):
        author_id = 1
        irc_n = display_name
    else:
        author_id = get_database_user(ctx.author).id
        irc_n = None

    new_announcement = Announcement(
        user_id=author_id,
        announcement_content=announcement_content,
        trigger_at=trigger_time,
        triggered=False,
        playback_channel_id=channel.id,
        irc_name=irc_n,
    )
    db_session.add(new_announcement)
    try:
        db_session.commit()
        gran = precisedelta(CONFIG.ANNOUNCEMENT_SEARCH_INTERVAL, minimum_unit="seconds")
        await ctx.send(
            f"Announcement prepared for <t:{int(trigger_time.timestamp())}:R> (granularity is {gran})."
        )

    except (ScalarListException, SQLAlchemyError) as e:
        db_session.rollback()
        logging.exception(e)
        await ctx.send(f"Something went wrong")


def setup(bot: Bot):
    bot.add_cog(Announcements(bot))