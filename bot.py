import os
import json
import asyncio
import socket
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

# ====== 環境変数 ======
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
# 公開URLなら https://xxx.up.railway.app （ポートなし）
# Private DNS を使うなら http://<service>.railway.internal:50021
VOICEVOX_URL = os.getenv("VOICEVOX_URL", "https://example.up.railway.app")

# デフォルト話者・スタイル（必要に応じて Variables で上書き）
DEFAULT_SPEAKER_NAME = os.getenv("VV_SPEAKER_NAME", "春日部つむぎ")
DEFAULT_STYLE_NAME   = os.getenv("VV_STYLE_NAME", "ノーマル")

# しゃきぴよ風プリセット（/vv reset 用）
SHAKIPIYO_PARAMS = dict(
    speedScale=1.15,
    pitchScale=0.60,
    intonationScale=1.20,
    volumeScale=1.0,
    prePhonemeLength=0.1,
    postPhonemeLength=0.1,
)

# 起動時の通常値（素の声にしたい時はここを 1.0/0.0/1.0 に）
DEFAULT_PARAMS = dict(
    speedScale=1.0,
    pitchScale=0.0,
    intonationScale=1.0,
    volumeScale=1.0,
    prePhonemeLength=0.1,
    postPhonemeLength=0.1,
)

# 即時同期ギルド（カンマ区切り、未設定ならグローバル同期）
GUILD_IDS = [int(x.strip()) for x in os.getenv("GUILD_IDS", "").split(",") if x.strip().isdigit()]

# ====== Bot 準備 ======
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# 再生キュー & タスク（ギルドごと）
voice_queues: dict[int, asyncio.Queue[bytes]] = {}
player_tasks: dict[int, asyncio.Task] = {}

# 現在のTTS設定
current_params = DEFAULT_PARAMS.copy()
current_speaker_name = DEFAULT_SPEAKER_NAME
current_style_name   = DEFAULT_STYLE_NAME


# ========= セッション（IPv4固定。Private DNS が IPv6-only なら公開URL https を推奨） =========
def _make_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(family=socket.AF_INET)  # IPv4のみ
    )


# ========= VOICEVOX Utility =========
async def resolve_speaker(session: aiohttp.ClientSession, name: str, style: str) -> int:
    async with session.get(f"{VOICEVOX_URL}/speakers") as r:
        r.raise_for_status()
        speakers = await r.json()
    for sp in speakers:
        if sp.get("name") == name:
            for st in sp.get("styles", []):
                if st.get("name") == style:
                    return st.get("id")
    return speakers[0]["styles"][0]["id"]  # fallback

async def synth_voicevox(text: str) -> bytes:
    async with _make_session() as session:
        spk_id = await resolve_speaker(session, current_speaker_name, current_style_name)

        # --- audio_query ---
        # 1st: 公式仕様（POST + クエリに text/speaker）
        async with session.post(
            f"{VOICEVOX_URL}/audio_query",
            params={"text": text, "speaker": spk_id},
        ) as r:
            if r.status == 200:
                query = await r.json()
            else:
                body = await r.text()
                # 405/415/422 の場合のみ json ボディ方式でフォールバック
                if r.status in (405, 415, 422):
                    async with session.post(
                        f"{VOICEVOX_URL}/audio_query",
                        params={"speaker": spk_id},
                        json={"text": text},
                        headers={"Accept": "application/json"},
                    ) as r2:
                        if r2.status != 200:
                            body2 = await r2.text()
                            raise RuntimeError(f"audio_query {r.status}/{r2.status}: {body} // {body2}")
                        query = await r2.json()
                else:
                    raise RuntimeError(f"audio_query {r.status}: {body}")

        # パラメータ反映（現在値）
        for k, v in current_params.items():
            query[k] = v

        # --- synthesis ---
        async with session.post(
            f"{VOICEVOX_URL}/synthesis",
            params={"speaker": spk_id},
            data=json.dumps(query),
            headers={"Content-Type": "application/json"},
        ) as r:
            if r.status != 200:
                body = await r.text()
                raise RuntimeError(f"synthesis {r.status}: {body}")
            return await r.read()  # wav bytes


# ========= 再生ループ（/tmp・Opus出力・クリーンアップ・競合回避） =========
async def ensure_player(vc: discord.VoiceClient):
    gid = vc.guild.id
    if gid in player_tasks and not player_tasks[gid].done():
        return
    if gid not in voice_queues:
        voice_queues[gid] = asyncio.Queue()

    async def _loop():
        try:
            while vc.is_connected():
                data = await voice_queues[gid].get()

                # すでに再生中なら終了を待つ（競合回避）
                while vc.is_playing():
                    await asyncio.sleep(0.1)

                # /tmp に一時wavを作成
                tmp = f"/tmp/vv_{gid}_{int(asyncio.get_event_loop().time())}.wav"
                with open(tmp, "wb") as f:
                    f.write(data)

                # FFmpeg -> Opus でDiscordへ（軽量・安定）
                source = discord.FFmpegOpusAudio(tmp)
                done_evt = asyncio.Event()

                def _after(_err):
                    try:
                        if hasattr(source, "cleanup"):
                            source.cleanup()
                    finally:
                        try:
                            os.remove(tmp)
                        except Exception:
                            pass
                        done_evt.set()

                vc.play(source, after=_after)
                await done_evt.wait()

        except Exception as e:
            print("[player_loop]", e)

    player_tasks[gid] = asyncio.create_task(_loop())


# ========= 安全なVC接続（4006対策：移動優先・リトライ） =========
async def safe_connect_to_user_channel(interaction: discord.Interaction, max_attempts: int = 3):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("先にVCへ入室してください。", ephemeral=True)
        return None

    target = interaction.user.voice.channel
    vc = interaction.guild.voice_client

    # 既に同じVC
    if vc and vc.is_connected() and vc.channel and vc.channel.id == target.id:
        if not interaction.response.is_done():
            await interaction.response.send_message(f"🔊 既に {target.mention} に接続済みです。", ephemeral=True)
        return vc

    # 別VCにいるなら move_to を優先
    if vc and vc.is_connected() and vc.channel and vc.channel.id != target.id:
        try:
            await vc.move_to(target)
            if not interaction.response.is_done():
                await interaction.response.send_message(f"↪️ {target.mention} に移動しました。")
            return vc
        except Exception:
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
            await asyncio.sleep(0.8)

    # 新規接続（4006 などはリトライ）
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            vc = await target.connect(timeout=8.0, reconnect=False)
            if not interaction.response.is_done():
                await interaction.response.send_message(f"🔊 {target.mention} に接続しました。")
            return vc
        except (discord.errors.ConnectionClosed, asyncio.TimeoutError) as e:
            last_err = e
            await asyncio.sleep(1.5 * attempt)
        except Exception as e:
            last_err = e
            break

    if not interaction.response.is_done():
        await interaction.response.send_message(f"⚠️ 接続に失敗しました: {type(last_err).__name__} {last_err}", ephemeral=True)
    return None


# ========= スラッシュコマンド =========
@bot.event
async def on_ready():
    print(f"Using VOICEVOX_URL={VOICEVOX_URL}")
    # 起動時疎通チェック
    try:
        async with _make_session() as s:
            async with s.get(f"{VOICEVOX_URL}/speakers", timeout=6) as r:
                r.raise_for_status()
        print(f"VOICEVOX OK: {VOICEVOX_URL}")
    except Exception as e:
        print(f"VOICEVOX NG: {VOICEVOX_URL} -> {e}")

    # コマンド同期
    try:
        if GUILD_IDS:
            for gid in GUILD_IDS:
                await tree.sync(guild=discord.Object(id=gid))
            print(f"Synced to guilds: {GUILD_IDS}")
        else:
            await tree.sync()
            print("Synced globally")
    except Exception as e:
        print("Sync error:", e)

    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@tree.command(name="sync", description="このサーバーにコマンドを即時同期（管理者専用）")
async def sync_here(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("管理者のみ実行可です。", ephemeral=True)
    await tree.sync(guild=interaction.guild)
    await interaction.response.send_message("✅ このサーバーに同期しました。", ephemeral=True)


@tree.command(name="join", description="あなたのいるVCに参加します。")
@app_commands.checks.bot_has_permissions(connect=True, speak=True)
async def join_cmd(interaction: discord.Interaction):
    vc = await safe_connect_to_user_channel(interaction)
    if vc:
        await ensure_player(vc)


@tree.command(name="leave", description="VCから退出します。")
async def leave_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc:
        return await interaction.response.send_message("未接続です。", ephemeral=True)
    try:
        await vc.disconnect(force=True)
    finally:
        await interaction.response.send_message("👋 切断しました。")
        await asyncio.sleep(0.8)  # セッション解放猶予


@tree.command(name="say", description="テキストを読み上げます。")
@app_commands.describe(text="読み上げる内容")
async def say_cmd(interaction: discord.Interaction, text: str):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        return await interaction.response.send_message("先に /join でVCに入れてください。", ephemeral=True)

    await interaction.response.defer(thinking=True, ephemeral=True)
    audio = await synth_voicevox(text)
    await voice_queues[interaction.guild.id].put(audio)
    await interaction.followup.send("📣 キューに追加しました。", ephemeral=True)


# VOICEVOX設定グループ
vv_group = app_commands.Group(name="vv", description="VOICEVOX設定")

@vv_group.command(name="voice", description="話者/スタイルを切替（例: 春日部つむぎ ノーマル）")
@app_commands.describe(speaker_name="話者名", style_name="スタイル名")
async def vv_voice(interaction: discord.Interaction, speaker_name: str, style_name: str):
    global current_speaker_name, current_style_name
    current_speaker_name = speaker_name
    current_style_name = style_name
    await interaction.response.send_message(f"🎙️ 声を `{speaker_name} / {style_name}` に変更しました。")

@vv_group.command(name="speed", description="話速 (0.5〜2.0)")
async def vv_speed(interaction: discord.Interaction, value: app_commands.Range[float, 0.5, 2.0]):
    current_params["speedScale"] = float(value)
    await interaction.response.send_message(f"⏩ speed = {current_params['speedScale']}")

@vv_group.command(name="pitch", description="ピッチ (-1.0〜1.0)")
async def vv_pitch(interaction: discord.Interaction, value: app_commands.Range[float, -1.0, 1.0]):
    current_params["pitchScale"] = float(value)
    await interaction.response.send_message(f"🎵 pitch = {current_params['pitchScale']}")

@vv_group.command(name="intonation", description="抑揚 (0.0〜2.0)")
async def vv_intonation(interaction: discord.Interaction, value: app_commands.Range[float, 0.0, 2.0]):
    current_params["intonationScale"] = float(value)
    await interaction.response.send_message(f"📈 intonation = {current_params['intonationScale']}")

@vv_group.command(name="reset", description="しゃきぴよ風プリセットにリセット")
async def vv_reset(interaction: discord.Interaction):
    global current_params
    current_params = SHAKIPIYO_PARAMS.copy()
    await interaction.response.send_message("♻️ パラメータをリセットしました（しゃきぴよ風）。")

tree.add_command(vv_group)

@tree.command(name="credit", description="利用中キャラクターのクレジットを表示します。")
async def credit_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"このBotは VOICEVOX:{current_speaker_name} の音声ライブラリを利用しています。"
    )

# ====== 起動 ======
if not DISCORD_TOKEN:
    raise RuntimeError("環境変数 DISCORD_TOKEN が未設定です。")
bot.run(DISCORD_TOKEN)
