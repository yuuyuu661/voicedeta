import os
import io
import json
import asyncio
import aiohttp
import discord
from discord.ext import commands

# ====== 環境変数 ======
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
VOICEVOX_URL = os.getenv("VOICEVOX_URL", "http://127.0.0.1:50021")
DEFAULT_SPEAKER_NAME = os.getenv("VV_SPEAKER_NAME", "春日部つむぎ")
DEFAULT_STYLE_NAME   = os.getenv("VV_STYLE_NAME", "ノーマル")

# ====== Bot準備 ======
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 再生キュー：連投でも順番に読み上げ
play_queue: "asyncio.Queue[bytes]" = asyncio.Queue()
player_task = None

# 「しゃきぴよ風」初期プリセット（高め・元気・やや早口）
SHAKIPIYO_PRESET = dict(
    speedScale=1.15,      # やや速い
    pitchScale=0.6,       # 高め
    intonationScale=1.2,  # 抑揚強め
    volumeScale=1.0,
    prePhonemeLength=0.1,
    postPhonemeLength=0.1,
)

current_params = SHAKIPIYO_PRESET.copy()
current_speaker_name = DEFAULT_SPEAKER_NAME
current_style_name   = DEFAULT_STYLE_NAME


# ====== VOICEVOXユーティリティ ======
async def resolve_speaker(session: aiohttp.ClientSession, name: str, style: str) -> int:
    async with session.get(f"{VOICEVOX_URL}/speakers") as r:
        speakers = await r.json()
    for sp in speakers:
        if sp.get("name") == name:
            for st in sp.get("styles", []):
                if st.get("name") == style:
                    return st.get("id")
    # 見つからなければ先頭のstyle
    return speakers[0]["styles"][0]["id"]

async def synth_voicevox(session: aiohttp.ClientSession, text: str) -> bytes:
    speaker_id = await resolve_speaker(session, current_speaker_name, current_style_name)
    async with session.post(
        f"{VOICEVOX_URL}/audio_query",
        params={"speaker": speaker_id},
        data=text.encode("utf-8"),
    ) as r:
        query = await r.json()

    # パラメータ上書き
    for k, v in current_params.items():
        query[k] = v

    async with session.post(
        f"{VOICEVOX_URL}/synthesis",
        params={"speaker": speaker_id},
        data=json.dumps(query),
        headers={"Content-Type": "application/json"},
    ) as r:
        return await r.read()  # wavバイト列


# ====== 再生ループ ======
async def player_loop(vc: discord.VoiceClient):
    """キューから取り出して1件ずつ再生"""
    try:
        while vc.is_connected():
            audio_bytes = await play_queue.get()  # 次の音声を待つ
            # 一時ファイルに保存してFFmpegで再生（Railwayでも安定）
            tmp = "vv_out.wav"
            with open(tmp, "wb") as f:
                f.write(audio_bytes)

            source = discord.FFmpegPCMAudio(
                tmp,  # VOICEVOXは24kHz mono wav → FFmpegが48kHz/stereoへリサンプル
            )
            done = asyncio.Event()

            def after_play(_):
                done.set()

            vc.play(source, after=after_play)
            await done.wait()
    except Exception as e:
        print("[player_loop] error:", e)


# ====== コマンド ======
@bot.command()
async def join(ctx: commands.Context):
    """呼んだ人のVCに参加"""
    if not ctx.author.voice:
        return await ctx.send("VCに入ってから呼んでください。")
    if ctx.voice_client and ctx.voice_client.is_connected():
        return await ctx.send("すでに接続済みです。")
    vc = await ctx.author.voice.channel.connect()
    await ctx.send(f"🔊 {ctx.author.voice.channel.mention} に接続しました。")

    global player_task
    if player_task is None or player_task.done():
        player_task = asyncio.create_task(player_loop(vc))


@bot.command()
async def leave(ctx: commands.Context):
    """VCから退出"""
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("👋 切断しました。")


@bot.command()
async def say(ctx: commands.Context, *, text: str):
    """テキストを合成して読み上げキューに投入"""
    if not ctx.voice_client or not ctx.voice_client.is_connected():
        return await ctx.send("先に `!join` でVCに入れてください。")
    async with aiohttp.ClientSession() as session:
        audio = await synth_voicevox(session, text)
    await play_queue.put(audio)
    await ctx.message.add_reaction("📣")


@bot.group(invoke_without_command=True)
async def vv(ctx: commands.Context):
    """VOICEVOX設定"""
    await ctx.send(
        "VOICEVOX設定:\n"
        "`!vv voice <speaker> <style>` / `!vv speed <0.5-2.0>` / "
        "`!vv pitch <-1.0-1.0>` / `!vv intonation <0.0-2.0>` / `!vv reset`\n"
        "例) `!vv voice 春日部つむぎ ノーマル`"
    )

@vv.command()
async def voice(ctx: commands.Context, speaker_name: str, style_name: str):
    global current_speaker_name, current_style_name
    current_speaker_name = speaker_name
    current_style_name = style_name
    await ctx.send(f"🎙️ 声を `{speaker_name} / {style_name}` に切替えました。")

@vv.command()
async def speed(ctx: commands.Context, value: float):
    current_params["speedScale"] = max(0.5, min(2.0, value))
    await ctx.send(f"⏩ speed = {current_params['speedScale']}")

@vv.command()
async def pitch(ctx: commands.Context, value: float):
    current_params["pitchScale"] = max(-1.0, min(1.0, value))
    await ctx.send(f"🎵 pitch = {current_params['pitchScale']}")

@vv.command()
async def intonation(ctx: commands.Context, value: float):
    current_params["intonationScale"] = max(0.0, min(2.0, value))
    await ctx.send(f"📈 intonation = {current_params['intonationScale']}")

@vv.command()
async def reset(ctx: commands.Context):
    global current_params
    current_params = SHAKIPIYO_PRESET.copy()
    await ctx.send("♻️ パラメータを『しゃきぴよ風』にリセットしました。")

# ====== 起動 ======
if not DISCORD_TOKEN:
    raise RuntimeError("環境変数 DISCORD_TOKEN が未設定です。")
bot.run(DISCORD_TOKEN)
