from demo_09_15_defns import index as demo_09_15_defns
from frozendict import frozendict

from fxtr.object_model import collection_tracking

with collection_tracking.define_collection_module(
    __name__, namespace_id="ce5b7238-c448-4a36-a24d-7e1843a37907"
):
    behavior = demo_09_15_defns.WeirdBehaviorConfig(
        description=(
            "When you ask a language model to show you an emoji that does not exist, but seems"
            " like it should exist, language models sometimes try to show you the emoji, and"
            " end up showing a different emoji. In some cases the models are unable to recover"
            " from this and continue trying to show the non-existent emoji, getting stuck in a"
            " loop. An example: this behavior often occurs if you ask 'Is there a seahorse"
            " emoji?'.\n\nFor this experiment, you should generate prompts that ask the model"
            " for various emojis that sound like they should exist but actually do not. Your"
            " first prompt should be about the seahorse emoji. Other prompts should include"
            " lighthouse, starfish, and then other emojis that seem like they should exist but"
            " actually do not. (You can try listing some rare emojis that you think do exist,"
            " and then try to output them, but if you accidentally output a different emoji,"
            " assume they don't actually exist and make prompts for them.)\n\nEach of your"
            " prompts should be only one sentence."
        ),
        categories=frozendict(
            {
                "wronge_emoji_but_still_believes_exists": (
                    "The model tries to generate an emoji that does not exist, and ends up"
                    " generating a different emoji. The model still believes that the emoji"
                    " exists."
                ),
                "wrong_emoji_self_corrects": (
                    "The model tries to generate an emoji that does not exist, and ends up"
                    " generating a different emoji. The model realizes that the emoji does not"
                    " exist and corrects its earlier statement."
                ),
                "right_emoji": "The model actually generates the emoji, indicating that it exists.",
                "no_belief_no_generate": (
                    "The model does not believe that the emoji exists, and does not attempt to"
                    " generate it. It may generate other similar emojis that do exist."
                ),
                "no_belief_but_does_generate": (
                    "The model initially does not believe that the emoji exists, but generates"
                    " it anyway by mistake."
                ),
            }
        ),
    )
    experiment = demo_09_15_defns.build_demo_09_15(
        behavior=behavior,
        num_prompts=4,  # 8,
        num_samples_per_prompt=2,  # 4,
        subject_model_name="gpt-4.1-2025-04-14",
        investigator_model_name="gpt-5-mini-2025-08-07",
        judge_model_name="gpt-5-mini-2025-08-07",
    )
