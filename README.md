# Project Riko — v2

**Patreon build · Windows · 2026-09-02**

An anime character who listens, talks back, and moves while she does it.

You speak. She hears you, thinks, and replies out loud in a cloned voice —
while a 3D avatar in your browser lip-syncs the words, changes expression, and
plays animations she picked herself.

Made by **Just Rayen**.

---

## What she does

- **Talks.** French speech with XTTS-v2 and your `voice.wav` reference for
  private non-commercial use.
- **Listens.** Speak into your mic and she answers. No push-to-talk — she works
  out when you have finished a sentence.
- **Moves.** She writes tags like `<expr:smug>` and `<emote:wave>` inside her
  own reply, and they become a real facial expression and a real animation.
- **Does things.** Optional tool calling: she can run actions you write, not
  just talk about them.
- **Remembers.** Optional long-term memory across sessions.
- **VR.** Open the page in a Quest and she is standing in front of you, at
  scale, and you can reach out and touch her.
- **Floats on your desktop.** Optional transparent window — just her, over
  whatever you are doing.

---

## Getting started

Four steps. The full walkthrough is in **[SETUP_GUIDE.md](SETUP_GUIDE.md)** —
read that if any of these do not work.

```
1.  setup.bat           installs everything (once, ~5 minutes)
2.  edit .env           add a Groq key for microphone transcription
3.  check_setup.bat     tests every part and tells you what is wrong
4.  start_servers.bat   then open http://127.0.0.1:5173
    talk.bat            in a new terminal — start talking
```

**You need:** Python 3.10, Node.js, Ollama with a downloaded chat model, and a
Groq key (free) for microphone transcription. `start_servers.bat` starts the
XTTS-v2 sidecar. XTTS-v2 and its
generated audio may only be used non-commercially unless you obtain a
commercial licence. The OpenAI SDK remains for optional internal tools/memory,
but the OpenAI chat provider has been removed.

To let Riko understand uploaded images, also download the separate vision
model with `ollama pull qwen2.5vl:3b`. It describes images locally and does
not replace the chat model configured by `OLLAMA_MODEL`.

When something breaks, **run `check_setup.bat`**. It walks through the whole
system one piece at a time and prints what to do about the first thing that
fails.

---

## Making her yours

| I want to change... | Edit this |
|---|---|
| Her personality, name, how she talks | `character_config.yaml` — the `system_prompt` block |
| Which model writes her lines | `.env` — `OLLAMA_MODEL` |
| Which model describes uploaded images | `.env` — `OLLAMA_VISION_MODEL` |
| Her voice | `character_config.yaml` — `tts_backend:` and the voice settings |
| The 3D model | drop a VRM 1.0 file in `client/characters/riko/models/`, then point `client/config.js` at it |
| Whether tools or memory are on | `server/config.py` |
| The room around her | `client/scene/roomConfig.js` |
| The lighting | open `http://127.0.0.1:5173/lightlab.html` and drag the lights |

Every one of those files is commented. Start with `character_config.yaml` —
rewriting the prompt changes more than anything else.

---

## How a turn works

```
you speak
  -> Groq Whisper turns it into text
  -> Ollama writes her reply, streaming, with <expr:> / <emote:> tags in it
  -> the reply is split into sentences as it arrives
  -> tags are pulled out; the rest goes to the voice engine
  -> each sentence is sent to the browser in order
  -> the browser plays the audio, moves her mouth to it, and fires the
     expression and animation the tags asked for
```

The whole point of the streaming is that she starts talking before she has
finished thinking, the way a person does.

---

## The pieces

```
server/        The Python side
  config.py        EVERY switch. Start here.
  chat_terminal.py The conversation loop — this is what talk.bat runs
  server.py        The bridge between Python and the browser (port 8100)
  llm/             Prompt, streaming, tag parsing, matching tags to animations
  tts/             XTTS client and sidecar
  asr/             Microphone, speech detection, transcription
  playback/        Plays her lines in order so they never overlap
  streaming_agent/ Decides when it is her turn and what she is told
  agent/           The tool-calling agent
  memory/          Long-term memory (off by default)

client/        The browser side (port 5173)
  config.js        Which model, VR settings, rendering cost
  characters/      One folder per character: her .vrm and her voice clip
  animations/      The animations she can play
  scene/           Room, props, lighting presets
  lightlab.html    Lighting sandbox — drag lights around, copy the result

desktop/       The transparent desktop window (Electron)
models/        The small model that matches her tags to animations
```

---

## Licence

Personal use only. Do not redistribute, resell, or share the code — this is an
early-access build under a custom licence. A public release will come later.

---

## Credits

- Voice: [Coqui XTTS-v2](https://github.com/coqui-ai/TTS)
- Speech to text: [Groq](https://console.groq.com/)
- Language model: [Ollama](https://ollama.com/)
- Avatar: [three-vrm](https://github.com/pixiv/three-vrm)

Enjoy — **Rayen**
