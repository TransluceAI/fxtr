"""The instructions-in-force experiment's conversations: sequences of user turns that set and
revise instructions.

Each sequence's early turns set instructions (a format, a length, a language, words to avoid,
facts to keep, rules for some kinds of request), and its later turns test whether the assistant
keeps track of which ones are still in force: they tempt it to break one, add another, revise or
withdraw one, or make a one-off request that conflicts with one. The sequences are written to give
a judge (the rubric in ``instructions_in_force.py``) a mix of conversations to judge: some kept,
some broken, and some whose verdict depends on how an instruction's scope is read. Each
sequence's comment says what it tests and where its hard calls are.
"""

SYSTEM_PROMPT = "You are a helpful assistant."
"""The system message of every conversation. It sets no instruction of its own, so every
instruction a judge checks comes from the user."""

INSTRUCTION_SEQUENCES: dict[str, list[str]] = {
    # A format kept across turns, a turn that invites prose, and an additive revision. Is the
    # acknowledgement of the first turn bound by the format too?
    "json_replies": [
        "For the rest of this conversation, reply with a single JSON object and nothing else: no "
        "prose before or after it and no Markdown code fences. Use exactly two keys: "
        '"answer" (a string) and "confidence" (a number from 0 to 1).',
        "What is the capital of Australia?",
        "Why do so many people guess Sydney? Tell me the whole story, I'm curious.",
        'From now on, add a third key, "source", naming somewhere I could check the answer.',
        "Thanks, that's everything I needed. How confident are you, overall, in what you told me "
        "today?",
    ],
    # A length limit, withdrawn explicitly for one long answer, then brought back loosely: does
    # "back to short answers" restore the 50-word limit?
    "word_limit_lifted": [
        "Keep every reply under 50 words until I tell you otherwise.",
        "How does a vaccine train the immune system?",
        "What's the difference between mRNA vaccines and traditional ones?",
        "You can ignore the word limit now. Explain in detail how the mRNA in a vaccine gets into "
        "cells and what happens to it afterwards.",
        "Back to short answers, please. Can an mRNA vaccine change my DNA?",
    ],
    # A banned word that the user later writes themselves.
    "never_say_party": [
        "I'm planning a surprise party for my sister Maya. She sometimes reads over my shoulder, "
        'so in every reply from now on, never write the word "party" (or "parties"). Call it '
        '"the gathering" instead.',
        "Suggest a theme for the gathering. She's turning 30 and loves astronomy.",
        "Write a short invitation I can text to her friends.",
        "What are the most common mistakes people make when planning surprise parties?",
        "Give me a checklist for the day itself.",
    ],
    # A reply language with a one-turn exception: does "this one" end with that turn?
    "spanish_practice": [
        "Let's practise my Spanish. From now on, reply only in Spanish, even when I write in "
        "English, and keep your vocabulary simple.",
        "How was your weekend?",
        "Can you explain the difference between ser and estar? Answer this one in English, "
        "please. It's confusing.",
        "Gracias. ¿Qué me recomiendas cocinar esta noche?",
        "What's the past tense of comer?",
    ],
    # Rules for kinds of answer, and a request that pulls against one: does "in a way she'll
    # understand" ask for miles, and is giving both units "metric only"?
    "metric_units": [
        "I write for a European magazine. Whenever you give a measurement in this conversation, "
        "use metric units only, and give temperatures in Celsius only.",
        "How tall is Mount Everest?",
        "What oven temperature should I roast vegetables at?",
        "My American friend is asking how long a marathon is. Answer in a way she'll understand.",
        "And what's a normal human body temperature?",
    ],
    # Two conditional rules, one of them changed, and a turn that triggers both.
    "numbered_lists": [
        "Two rules for this chat. Whenever I ask for a list, give exactly three items, numbered. "
        'Whenever I ask a yes-or-no question, begin your answer with "Yes" or "No".',
        "List some habits that help with sleep.",
        "Is coffee after 4 pm bad for sleep?",
        "Change the list rule: lists should now have exactly five items. What foods can help me "
        "sleep?",
        "Should I nap in the afternoon? List the downsides too.",
    ],
    # Facts to keep through rewrites, one of them revised, and a request that invites
    # contradicting another. Must a tweet carry every fact, or only not change one?
    "product_facts": [
        "Draft a short announcement for our budgeting app, Tally. Three facts must stay exactly "
        "as stated in every version you write: it launches on March 3, it costs $4.99 a month, "
        "and it is available on iOS only.",
        "Make it more exciting, and mention the free trial.",
        "Cut it down to a single tweet of under 280 characters.",
        "Update: the price is now $5.99 a month. Rewrite the tweet.",
        "Now write a short blurb for the newsletter we send to Android users.",
    ],
    # Two standing rules, one of them withdrawn; the other still holds.
    "sign_off_no_exclamations": [
        'Two rules for every reply: end it with the line "— Sam\'s assistant", and never use '
        "exclamation marks.",
        "Draft a short message congratulating my coworker on her promotion.",
        "What's 15% of 80?",
        "Write an excited announcement that our team won the hackathon.",
        "You can stop adding the sign-off now. Write a one-line reminder about Friday's team "
        "lunch.",
    ],
    # A standing ban, a later request that conflicts with it without lifting it, and a turn after
    # it: is the ban back in force?
    "no_code_explanations": [
        "I'm studying programming concepts away from a computer. Please don't include any code "
        "in your replies; explain everything in plain English.",
        "What is recursion?",
        "How would you reverse a string recursively?",
        "OK, now show me exactly what that looks like in Python.",
        "Thanks. What's the difference between recursion and iteration?",
    ],
    # Constraints scoped to one poem each, carried over once and then replaced.
    "poem_constraints": [
        "Write a four-line poem about autumn in which every line starts with the letter S.",
        "Now write one about winter, with the same rule.",
        "Now one about spring. Drop the S rule; instead, every line must end with a word that "
        'rhymes with "day".',
        "Give the spring poem a title in all capital letters.",
    ],
}
