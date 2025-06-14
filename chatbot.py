import os
os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY

import datetime
import logging
import omegaconf
from openai import OpenAI
from pydub import AudioSegment
import telebot
import json
import yaml
import subprocess

import logging
logging.getLogger('RootLogger').setLevel(logging.DEBUG)

# Logging and configuration
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

# Telegram Bot and OpenAI client
bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = OpenAI()

def convert_to_voice(input_path: str, output_path: str) -> int:
    command = [
        "ffmpeg",
        "-i", input_path,
        "-c:a", "libopus",
        "-b:a", "32k",
        "-vbr", "on",
        "-compression_level", "10",
        "-frame_duration", "60",
        "-application", "voip",
        output_path
    ]
    return_code = 0
    try:
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        print(f"Command failed with error {e.returncode}")
        logging.error(f"Command failed with error {e.returncode}")
        logging.error(f"stdout:\n{e.stdout.decode()}")
        logging.error(f"stderr:\n{e.stderr.decode()}")
    finally:
        return return_code


def get_message_history(message):
    chat_dir = os.path.join("chats")
    os.makedirs(chat_dir, exist_ok=True)
    chat_log_file = os.path.join(chat_dir, f"{message.from_user.username}.yaml")
    if not os.path.exists(chat_log_file):
        return []
    with open(chat_log_file, "r") as chat_log:
        chat_log_openai_format = []
        try:
            yaml_data = yaml.safe_load(chat_log)
            for message_log in yaml_data:
                chat_log_openai_format.append({
                    "role": "assistant" if message_log.get("from") == "openai_assistant" else "user",
                    "content": message_log.get("text", ""),
                })
        except yaml.YAMLError as exc:
            logging.error(exc)
        finally:
            return chat_log_openai_format


def add_messages_to_history(message, user_text, agent_text):
    chat_dir = os.path.join("chats")
    os.makedirs(chat_dir, exist_ok=True)
    chat_log_file = os.path.join(chat_dir, f"{message.from_user.username}.yaml")
    with open(chat_log_file, "a") as chat_log:
        json_lines = [
            "- " + json.dumps({
                "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "message_id": message.message_id,
                "from": message.from_user.username,
                "text": user_text,
            }),
            "- " + json.dumps({
                "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "message_id": message.message_id,
                "from": "openai_assistant",
                "text": agent_text,
            }),
        ]
        chat_log.write('\n'.join(json_lines) + '\n')


@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    logging.info(f"Received message from {message.from_user.username}: {message.text}")
    if message.from_user.username not in USER_WHITELIST:
        bot.reply_to(message, "Sorry, you are not allowed to use this bot. Please contact @hav4ik for access.")
        return
    bot.reply_to(message, open("README.md", "r").read(), parse_mode='Markdown')


@bot.message_handler(commands=['prompt'])
def send_prompt(message):
    logging.info(f"Received message from {message.from_user.username}: {message.text}")
    if message.from_user.username not in USER_WHITELIST:
        bot.reply_to(message, "Sorry, you are not allowed to use this bot. Please contact @hav4ik for access.")
        return
    bot.reply_to(message, CHAT_SYSTEM_PROMPTS[message.from_user.username])


@bot.message_handler(content_types=['voice'])
def voice_processing(message):
    logging.info(f"Received voice message from {message.from_user.username}")
    if message.from_user.username not in USER_WHITELIST:
        bot.reply_to(message, "Sorry, you are not allowed to use this bot. Please contact @hav4ik for access.")
        return
    # Prepare logging storage
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    temp_dir = "temp"
    os.makedirs(temp_dir, exist_ok=True)
    unique_filename = os.path.join(temp_dir, f"{message.from_user.username}-{message.message_id:010d}")

    # Download the file
    file_info = bot.get_file(message.voice.file_id)
    logging.info(f"  - Downloading file {file_info.file_path}")
    downloaded_bytes = bot.download_file(file_info.file_path)
    with open(unique_filename + ".ogg", 'wb') as new_file:
        new_file.write(downloaded_bytes)

    # Load OGG audio from the byte stream
    audio = AudioSegment.from_file(unique_filename + ".ogg", format="ogg")
    audio.export(unique_filename + ".mp3", format="mp3")
    logging.info(f"  - Saved audio to {unique_filename}.mp3")

    # Transcribe the audio with OpenAI Whishper
    audio_mp3_bytes = open(unique_filename + ".mp3", "rb")
    transcription = client.audio.transcriptions.create(
        model="gpt-4o-transcribe",
        file=open(unique_filename + ".mp3", "rb"))
    # if not "text" in transcription:
    #     bot.reply_to(message, "Sorry, something went wrong. Please contact @hav4ik for help or try again.")
    #     return
    # transcript_text = transcription['text']
    transcript_text = transcription.text
    logging.info(f"  - Transcript: {transcript_text}")

    # Cleanup temp files
    os.remove(unique_filename + ".ogg")
    os.remove(unique_filename + ".mp3")

    # Open chat log and get latest messages (in OpenAI API format compatible with chat models)
    chatgpt_inputs = [
        {"role": "system", "content": CHAT_SYSTEM_PROMPTS[message.from_user.username]},
    ]
    chat_history = get_message_history(message)[-CHAT_CONFIG['max_history']:]
    chatgpt_inputs.extend(chat_history)
    chatgpt_inputs.append({"role": "user", "content": transcript_text})
    chatgpt_response = client.chat.completions.create(
        model="gpt-4.1-nano",
        messages=chatgpt_inputs,
    )
    if len(chatgpt_response.choices) == 0:
        logging.error(f"  - ChatGPT response is empty")
        bot.reply_to(message, "Sorry, something went wrong with OpenAI API call. Please contact @hav4ik for help or try again.")
        return
    chatgpt_response_text = chatgpt_response.choices[0].message.content
    logging.info(f"  - ChatGPT response: {chatgpt_response_text}")

    # Update Chat logs
    add_messages_to_history(message, user_text=transcript_text, agent_text=chatgpt_response_text)

    # OpenAI TTS
    with client.audio.speech.with_streaming_response.create(
        model="gpt-4o-mini-tts",
        voice="coral",
        input=chatgpt_response_text,
        instructions="Speak in a cheerful and positive tone.",
    ) as response:
        response.stream_to_file(f"{unique_filename}_response.wav")

    # Convert to OGG format
    if not convert_to_voice(f"{unique_filename}_response.wav", f"{unique_filename}_response.ogg") == 0:
        logging.error(f"  - Failed to convert audio to OGG")
        bot.reply_to(message, "Sorry, something went wrong during speech synthesis. Please contact @hav4ik for help or try again.")
        return
    else:
        logging.info(f"  - Converted audio to {unique_filename}_response.ogg")

    # Send the response
    bot.send_voice(message.chat.id, voice=open(f"{unique_filename}_response.ogg", "rb"))

    # Cleanup temp files
    os.remove(f"{unique_filename}_response.wav")
    os.remove(f"{unique_filename}_response.ogg")


@bot.message_handler(content_types=['text'])
def handle_chat(message):
    logging.info(f"Received text message from {message.from_user.username}: {message.text}")
    if message.from_user.username not in USER_WHITELIST:
        bot.reply_to(message, "Sorry, you are not allowed to use this bot. Please contact @hav4ik for access.")
        return

    # Open chat log and get latest messages (in OpenAI API format compatible with chat models)
    chatgpt_inputs = [
        {"role": "system", "content": CHAT_SYSTEM_PROMPTS[message.from_user.username]},
    ]
    chat_history = get_message_history(message)[-CHAT_CONFIG['max_history']:]
    chatgpt_inputs.extend(chat_history)
    chatgpt_inputs.append({"role": "user", "content": message.text})
    chatgpt_response = client.chat.completions.create(
        model="gpt-4.1-nano",
        messages=chatgpt_inputs,
    )
    chatgpt_response_text = chatgpt_response.choices[0].message.content
    logging.info(f"  - ChatGPT response: {chatgpt_response_text}")

    # Update Chat logs
    add_messages_to_history(message, user_text=message.text, agent_text=chatgpt_response_text)

    # Send the response
    bot.reply_to(message, chatgpt_response_text)


bot.infinity_polling()
