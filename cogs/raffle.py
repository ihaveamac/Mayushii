import asyncio
import discord
import random

from discord.ext import commands
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine
from utils.checks import not_new, not_blacklisted
from utils.database import Giveaway, Entry, GiveawayRole, BlackList
from utils.exceptions import NoOnGoingRaffle
from utils.utilities import wait_for_answer
from typing import List

Base = declarative_base()


class Raffle(commands.Cog):
    """Giveaway commands for giveaway use"""

    def __init__(self, bot):
        self.bot = bot
        self.logger = self.bot.get_logger(self)
        engine = create_engine("sqlite:///giveaway.db")
        session = sessionmaker(bind=engine)
        self.s = session()
        Base.metadata.create_all(
            engine,
            tables=[
                Giveaway.__table__,
                Entry.__table__,
                GiveawayRole.__table__,
                BlackList.__table__,
            ],
        )
        self.s.commit()
        self.raffle = self.s.query(Giveaway).filter_by(ongoing=True).scalar()
        self.roles = []
        if self.raffle and self.raffle.roles:
            for role in self.raffle.roles:
                if (drole := discord.utils.get(bot.guild.roles, id=role.id)) is None:
                    self.s.query(GiveawayRole).get((role.id, self.raffle.id)).delete()
                    self.s.commit()
                else:
                    self.roles.append(drole)
        self.queue = asyncio.Queue()

    # checks
    def ongoing_raffle(ctx: commands.Context):
        if ctx.cog.raffle and ctx.cog.raffle.ongoing:
            return True
        raise NoOnGoingRaffle("There is no ongoing raffle.")

    # internal functions
    async def queue_empty(self):
        while not self.queue.empty():
            pass

    async def process_entry(self):
        ctx = await self.queue.get()
        if self.raffle.roles:
            if not (
                any(role in ctx.author.roles for role in self.roles)
                or any(
                    discord.utils.get(ctx.author.roles, id=role_id)
                    for role_id in self.bot.config["default_roles"]
                )
            ):
                return await ctx.send("You are not allowed to participate!")
        user_id = ctx.author.id
        entry = self.s.query(Entry).get((user_id, self.raffle.id))
        if entry:
            return await ctx.send("You are already participating!")
        self.s.add(Entry(id=user_id, giveaway=self.raffle.id))
        self.s.commit()
        await ctx.send(f"{ctx.author.mention} now you are participating in the raffle!")
        self.queue.task_done()

    def create_raffle(self, name: str, winners: int, roles: List[int]):
        raffle = Giveaway(name=name, win_count=winners)
        self.s.add(raffle)
        self.s.commit()
        if roles:
            self.s.add_all(
                [GiveawayRole(id=role_id, giveaway=raffle.id) for role_id in roles]
            )
            self.s.commit()
            self.roles = [
                discord.utils.get(self.bot.guild.roles, id=role_id)
                for role_id in self.raffle.roles
            ]
        return raffle

    def get_winner(self):
        while len(self.raffle.entries) >= 1:
            entry = random.choice(self.raffle.entries)
            if (winner := self.bot.guild.get_member(entry.id)) is not None:
                entry.winner = True
                self.s.commit()
                return winner
            self.s.delete(entry)
        return None

    @commands.check(not_blacklisted)
    @commands.check(not_new)
    @commands.check(ongoing_raffle)
    @commands.guild_only()
    @commands.command()
    async def join(self, ctx):
        await self.queue.put(ctx)
        await self.process_entry()

    @commands.guild_only()
    @commands.group(aliases=["raffle"])
    async def giveaway(self, ctx):
        """Giveaway related commands."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @commands.has_guild_permissions(manage_channels=True)
    @commands.guild_only()
    @giveaway.command()
    async def create(
        self,
        ctx,
        name: str,
        winners: int = 1,
        roles: commands.Greedy[discord.Role] = None,
    ):
        """Creates a giveaway"""
        if self.raffle:
            return await ctx.send("There is an already ongoing giveaway!")
        embed = discord.Embed(title="Proposed Giveaway", color=discord.Color.purple())
        embed.add_field(name="Name", value=name, inline=False)
        embed.add_field(name="Number of winners", value=str(winners), inline=False)
        if roles:
            embed.add_field(
                name="Roles accepted",
                value=" ".join(role.name for role in roles),
                inline=False,
            )
        await ctx.send(
            "Say `yes` to confirm giveaway creation, `no` to cancel", embed=embed
        )
        if await wait_for_answer(ctx):
            self.raffle = self.create_raffle(
                name, winners, [role.id for role in roles] if roles else []
            )
            await ctx.send(
                f"Started giveaway {name} with {winners} possible winners! Use `{self.bot.command_prefix}join`to join"
            )
        else:
            await ctx.send("Alright then.")

    @commands.has_guild_permissions(manage_channels=True)
    @commands.check(ongoing_raffle)
    @commands.guild_only()
    @giveaway.command()
    async def info(self, ctx):
        """Shows information about current giveaway"""
        embed = discord.Embed()
        embed.add_field(name="ID", value=self.raffle.id, inline=False)
        embed.add_field(name="Name", value=self.raffle.name, inline=False)
        if self.roles:
            embed.add_field(
                name="Allowed Roles",
                value="\n".join(role.name for role in self.roles),
                inline=False,
            )
        embed.add_field(
            name="Number of entries", value=str(len(self.raffle.entries)), inline=False
        )
        await ctx.send(embed=embed)

    @commands.has_guild_permissions(manage_channels=True)
    @commands.check(ongoing_raffle)
    @commands.guild_only()
    @giveaway.command()
    async def cancel(self, ctx):
        """Cancels current giveaway"""
        await ctx.send("Are you sure you want to cancel current giveaway?")
        if await wait_for_answer(ctx):
            self.raffle.ongoing = False
            self.raffle = None
            self.s.commit()
            return await ctx.send("Giveaway cancelled.")
        await ctx.send("And the raffle continues.")

    @commands.has_guild_permissions(manage_channels=True)
    @commands.check(ongoing_raffle)
    @commands.guild_only()
    @giveaway.command()
    async def finish(self, ctx):
        self.raffle.ongoing = False
        self.s.commit()
        await asyncio.wait_for(self.queue_empty(), timeout=None)
        winners = []
        for i in range(0, self.raffle.win_count):
            winners.append(self.get_winner())
        winners = list(filter(lambda a: a is not None, winners))
        if len(winners) < self.raffle.win_count:
            await ctx.send("Not enough participants for giveaway!")
            if not winners:
                await ctx.send("No users to choose...")
                self.raffle.ongoing = True
                return
        await ctx.send("And the winner is....!!")
        async with ctx.channel.typing():
            await asyncio.sleep(5)
            for user in winners:
                await ctx.send(f"{user.mention}")
            await ctx.send("Congratulations")
        for user in winners:
            try:
                await user.send(f"You're the {self.raffle.name} raffle winner!!")
            except (discord.HTTPException, discord.Forbidden):
                await ctx.send(f"Failed to send message to winner {user.mention}!")
        self.s.commit()
        await ctx.send(
            f"Giveaway finished with {len(self.raffle.entries)} participants."
        )
        self.raffle = None

    @commands.has_guild_permissions(manage_nicknames=True)
    @commands.guild_only()
    @giveaway.group()
    async def blacklist(self, ctx):
        """Commands for managing the blacklist"""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @blacklist.command()
    async def add(self, ctx, member: discord.Member):
        """Blacklist user"""
        if self.s.query(BlackList).get(member.id):
            await ctx.send(f"{member} is already in the blacklist")
            return
        self.s.add(BlackList(userid=member.id))
        self.s.commit()
        await ctx.send(f"Added {member} to the blacklist")

    @blacklist.command()
    async def remove(self, ctx, member: discord.Member):
        """Removes user from Blacklist"""
        if not (entry := self.s.query(BlackList).get(member.id)):
            await ctx.send(f"{member} is not in the blacklist.")
            return
        self.s.delete(entry)
        self.s.commit()
        await ctx.send(f"Removed {member} from the blacklist")

    @commands.check(ongoing_raffle)
    @commands.guild_only()
    @giveaway.group()
    async def modify(self, ctx):
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @commands.has_guild_permissions(manage_channels=True)
    @modify.command()
    async def winner_count(self, ctx, value: int):
        """Modify a parameter of the raffle"""
        self.raffle.win_count = value
        self.s.commit()
        await ctx.send(f"Updated number of winners to {value}")


def setup(bot):
    bot.add_cog(Raffle(bot))
