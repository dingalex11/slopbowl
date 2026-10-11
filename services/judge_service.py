import os
import json
import asyncio
import base64
from openai import AsyncOpenAI

class JudgeService:
    # Require api_key in the initializer
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("[JUDGE ERROR]: API Key is missing!")

        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://api.groq.com/openai/v1"
        )
        print("[JUDGE AGENT]: Initialized online Groq AI Judge.")
        
    # ... KEEP ALL OTHER METHODS THE SAME ...

    async def transcribe_audio(self, b64_audio: str) -> str:
        """Sends raw audio bytes to Groq's Whisper API for transcription."""
        if not b64_audio:
            return ""
            
        try:
            # Strip the base64 data URI header if the browser sent it
            if "," in b64_audio:
                b64_audio = b64_audio.split(",")[1]
                
            audio_bytes = base64.b64decode(b64_audio)
            
            # Whisper API needs a tuple with a filename to determine the audio format.
            audio_file = ("audio.webm", audio_bytes, "audio/webm")
            
            # System prompt to steer Whisper towards Science Bowl output rules
            stt_prompt = (
                "Quiz Review: For Question 1, the correct answer is option (W). For Question 2, choice X. For Question 3, select (Y), and for Question 4, option Z. Calculate values: 3.00 × 10^8 m/s, 6.022 × 10^23 mol^-1, 1.602 × 10^-19 C, and 9.11 × 10^-31 kg."
            )
            
            response = await self.client.audio.transcriptions.create(
                file=audio_file,
                model="whisper-large-v3", # Groq's flagship STT model
                response_format="json",
                language="en",
                temperature=0.0,
                prompt=stt_prompt # Injected prompt context
            )
            
            transcript = response.text.strip()
            print(f"[STT AGENT]: Heard -> '{transcript}'")
            return transcript
            
        except Exception as e:
            print(f"❌ [STT ERROR]: {e}")
            return ""

    async def evaluate_answer(
        self, 
        spoken_answer: str, 
        correct_answer: str, 
        category: str, 
        is_multiple_choice: bool = False
    ) -> bool:
        """Queries Groq Llama 3 to evaluate scientific answer equivalence."""
        
        if not spoken_answer or not spoken_answer.strip():
            print(f"\n[JUDGE AGENT]: Evaluating '' against '{correct_answer}'...")
            print("[JUDGE AGENT]: Result -> False (No answer spoken / empty input)")
            return False

        print(f"\n[JUDGE AGENT]: Evaluating '{spoken_answer}' against '{correct_answer}'...")

        system_prompt = (
            "You are an official National Science Bowl (NSB) Judge evaluating speech-to-text (STT) student audio transcripts. "
            "You must strictly enforce official NSB rules while distinguishing between genuine student errors and automated transcription artifacts.\n\n"
            "OFFICIAL SCIENCE BOWL & TRANSCRIPTION RULES:\n\n"
            "1. MULTIPLE CHOICE RULES (Choices: W, X, Y, Z):\n"
            "   - Acceptable Formats: Letter alone, choice text alone, or both combined (e.g., 'W', 'stationary', 'W stationary').\n"
            "   - STT Letter Homophones: Map phonetic transcriptions to their respective letters:\n"
            "       * 'double you', 'double-u', 'w.' -> W\n"
            "       * 'ex', 'axe', 'x.' -> X\n"
            "       * 'why', 'y.' -> Y\n"
            "       * 'zee', 'zed', 'z.' -> Z\n"
            "   - Punctuation Artifacts: Treat 'W. Stationary', 'W, stationary', or 'Choice W: stationary' as valid combined answers.\n"
            "   - Conflict Rule: If the student gives a choice letter that contradicts the text they speak (e.g., 'X stationary' when W is stationary), mark FALSE.\n\n"
            "2. SHORT ANSWER & SCIENTIFIC FORMATTING:\n"
            "   - Equivalence: Treat standard mathematical, chemical, and notation formats as identical:\n"
            "       * Spoken numbers vs. digits: 'forty two' == '42', 'zero point five' == '0.5' == '1/2'.\n"
            "       * Signs: 'negative three' == 'minus three' == '-3'.\n"
            "       * Scientific notation: 'three times ten to the eighth' == '3 x 10^8' == '3e8'.\n"
            "       * Chemical formulas/names: 'H2O' == 'water' (unless the key explicitly requires chemical formula vs. common name).\n"
            "   - Phonetic STT Near-Misses: Accept obvious phonetic mistranscriptions of technical terms where the acoustics match the key and no conflicting term exists (e.g., 'jewels' for 'joules', 'cool ohm' for 'coulomb', 'a leels' for 'alleles', 'my toes is' for 'mitosis').\n"
            "   - Answer Directives:\n"
            "       * Honor '(ACCEPT: ...)' or '[ACCEPT: ...]' variants.\n"
            "       * Honor '(DO NOT ACCEPT: ...)' or '[DO NOT ACCEPT: ...]' strictly—if the transcript contains a prohibited term, mark FALSE.\n\n"
            "3. CONVERSATIONAL FILLERS VS. CONTRADICTIONS:\n"
            "   - Conversational Fillers & Preamble: Strip speech fillers and leading phrases before judging (e.g., 'um', 'uh', 'is it...', 'the answer is...', 'it's gotta be...').\n"
            "   - Stutters & Acoustic Repeats: Ignore transcript stuttering (e.g., 'cell cell membrane' -> 'cell membrane').\n"
            "   - Contradictory Answers: If the student states two conflicting scientific terms, changes choices, or self-corrects (e.g., 'W wait X', 'carbon no oxygen'), mark FALSE.\n"
            "   - Non-Answers / Hallucinations: Non-attempts ('idk', 'pass', 'oops') or empty/ambient noise outputs ('thank you for watching') are ALWAYS FALSE.\n\n"
            "OUTPUT REQUIREMENT:\n"
            "Respond strictly in valid JSON format:\n"
            "{\"is_correct\": true/false, \"reason\": \"concise ruling referencing the answer key and applicable rule\"}"
        )

        user_prompt = f"Category: {category}\nOfficial Answer: {correct_answer}\nStudent Spoken Answer: {spoken_answer}"

        try:
            response = await self.client.chat.completions.create(
                model="openai/gpt-oss-20b",
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.0
            )

            result = json.loads(response.choices[0].message.content)
            print(f"[JUDGE AGENT]: Result -> {result.get('is_correct', False)} ({result.get('reason', '')})")
            return result.get("is_correct", False)

        except Exception as e:
            print(f"[JUDGE AGENT ERROR]: {e}")
            return False