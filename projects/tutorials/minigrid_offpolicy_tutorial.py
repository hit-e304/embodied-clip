# literate: tutorials/offpolicy-tutorial.md
# %%
"""# Tutorial: Off-policy training."""

# %%
"""

**Note** The provided commands to execute in this tutorial assume you have
[installed the full library](../installation/installation-allenact.md#full-library) and the `extra_requirements`
for the `babyai_plugin` and `minigrid_plugin`. The latter can be installed with:

```bash
pip install -r allenact_plugins/babyai_plugin/extra_requirements.txt; pip install -r allenact_plugins/minigrid_plugin/extra_requirements.txt
```

In this tutorial we'll learn how to train an agent from an external dataset by imitating expert actions via
Behavior Cloning. We'll use a [BabyAI agent](/api/allenact_plugins/babyai_plugin/babyai_models#BabyAIRecurrentACModel) to solve
`GoToLocal` tasks on [MiniGrid](https://github.com/maximecb/gym-minigrid); see the
`projects/babyai_baselines/experiments/go_to_local` directory for more details.

This tutorial assumes `AllenAct`'s [abstractions](../getting_started/abstractions.md) are known.

## The task

In a `GoToLocal` task, the agent immersed in a grid world has to navigate to a specific object in the presence of
multiple distractors, requiring the agent to understand `go to` instructions like "go to the red ball". For further
details, please consult the [original paper](https://arxiv.org/abs/1810.08272).

## Getting the dataset

We will use a large dataset (**more than 4 GB**) including expert demonstrations for `GoToLocal` tasks. To download
the data we'll run

```bash
PYTHONPATH=. python allenact_plugins/babyai_plugin/scripts/download_babyai_expert_demos.py GoToLocal
```

from the project's root directory, which will download `BabyAI-GoToLocal-v0.pkl` and `BabyAI-GoToLocal-v0_valid.pkl` to
the `allenact_plugins/babyai_plugin/data/demos` directory.

We will also generate small versions of the datasets, which will be useful if running on CPU, by calling

```bash
PYTHONPATH=. python allenact_plugins/babyai_plugin/scripts/truncate_expert_demos.py
```
from the project's root directory, which will generate `BabyAI-GoToLocal-v0-small.pkl` under the same
`allenact_plugins/babyai_plugin/data/demos` directory.

## Data storage

In order to train with an off-policy dataset, we need to define an `ExperienceStorage`. In AllenAct, an
 `ExperienceStorage` object has two primary functions:
1. It stores/manages relevant data (e.g. similarly to the `Dataset` class in PyTorch).
2. It loads stored data into batches that will be used for loss computation (e.g. similarly to the `Dataloader` 
class in PyTorch).
Unlike a PyTorch `Dataset` however, an `ExperienceStorage` object can build its dataset **at runtime** by processing
 rollouts from the agent. This flexibility allows for us to, for exmaple, implement the experience replay datastructure
 used in deep Q-learning. For this tutorial we won't need this additional functionality as our off-policy dataset
 is a fixed collection of expert trajectories.    

An example of a `ExperienceStorage` for BabyAI expert demos might look as follows:
"""

# %% import_summary allenact_plugins.minigrid_plugin.minigrid_offpolicy.MiniGridExpertTrajectoryStorage

# %%
"""
A complete example can be found in
[MiniGridExpertTrajectoryStorage](/api/allenact_plugins/minigrid_plugin/minigrid_offpolicy#MiniGridExpertTrajectoryStorage).

## Loss function

Off-policy losses must implement the
[`GenericAbstractLoss`](/api/allenact/base_abstractions/misc/#genericabstractloss)
interface. In this case, we minimize the cross-entropy between the actor's policy and the expert action:
"""

# %% import allenact_plugins.minigrid_plugin.minigrid_offpolicy.MiniGridOffPolicyExpertCELoss

# %%
"""
A complete example can be found in
[MiniGridOffPolicyExpertCELoss](/api/allenact_plugins/minigrid_plugin/minigrid_offpolicy#MiniGridOffPolicyExpertCELoss).
Note that in this case we train the entire actor, but it would also be possible to forward data through a different
subgraph of the ActorCriticModel.

## Experiment configuration

For the experiment configuration, we'll build on top of an existing
[base BabyAI GoToLocal Experiment Config](/api/projects/babyai_baselines/experiments/go_to_local/base/#basebabyaigotolocalexperimentconfig).
The complete `ExperimentConfig` file for off-policy training is
[here](/api/projects/tutorials/minigrid_offpolicy_tutorial/#bcoffpolicybabyaigotolocalexperimentconfig), but let's
focus on the most relevant aspect to enable this type of training:
providing an [OffPolicyPipelineComponent](/api/allenact/utils/experiment_utils/#offpolicypipelinecomponent) object as input to a
`PipelineStage` when instantiating the `TrainingPipeline` in the `training_pipeline` method.
"""

# %% hide
import os
from typing import Optional, List, Tuple

import torch
from gym_minigrid.minigrid import MiniGridEnv

from allenact.algorithms.onpolicy_sync.storage import RolloutBlockStorage
from allenact.utils.experiment_utils import (
    PipelineStage,
    StageComponent,
    TrainingSettings,
)
from allenact_plugins.babyai_plugin.babyai_constants import (
    BABYAI_EXPERT_TRAJECTORIES_DIR,
)
from allenact_plugins.minigrid_plugin.minigrid_offpolicy import (
    MiniGridOffPolicyExpertCELoss,
    MiniGridExpertTrajectoryStorage,
)
from projects.babyai_baselines.experiments.go_to_local.base import (
    BaseBabyAIGoToLocalExperimentConfig,
)


# %%
class BCOffPolicyBabyAIGoToLocalExperimentConfig(BaseBabyAIGoToLocalExperimentConfig):
    """BC Off-policy imitation."""

    DATASET: Optional[List[Tuple[str, bytes, List[int], MiniGridEnv.Actions]]] = None

    GPU_ID = 0 if torch.cuda.is_available() else None

    @classmethod
    def tag(cls):
        return "BabyAIGoToLocalBCOffPolicy"

    @classmethod
    def METRIC_ACCUMULATE_INTERVAL(cls):
        # See BaseBabyAIGoToLocalExperimentConfig for how this is used.
        return 1

    @classmethod
    def training_pipeline(cls, **kwargs):
        total_train_steps = cls.TOTAL_IL_TRAIN_STEPS
        ppo_info = cls.rl_loss_default("ppo", steps=-1)

        num_mini_batch = ppo_info["num_mini_batch"]
        update_repeats = ppo_info["update_repeats"]

        # fmt: off
        return cls._training_pipeline(
            named_losses={
                "offpolicy_expert_ce_loss": MiniGridOffPolicyExpertCELoss(
                    total_episodes_in_epoch=int(1e6)
                ),
            },
            named_storages={
                "onpolicy": RolloutBlockStorage(),
                "minigrid_offpolicy_expert": MiniGridExpertTrajectoryStorage(
                    data_path=os.path.join(
                                BABYAI_EXPERT_TRAJECTORIES_DIR,
                                "BabyAI-GoToLocal-v0{}.pkl".format(
                                    "" if torch.cuda.is_available() else "-small"
                                ),
                            ),
                    num_samplers=cls.NUM_TRAIN_SAMPLERS,
                    rollout_len=cls.ROLLOUT_STEPS,
                    instr_len=cls.INSTR_LEN,
                ),
            },
            pipeline_stages=[
                # Single stage, only with off-policy training
                PipelineStage(
                    loss_names=["offpolicy_expert_ce_loss"],                                              # no on-policy losses
                    max_stage_steps=total_train_steps,                          # keep sampling episodes in the stage
                    stage_components=[
                        StageComponent(
                            uuid="offpolicy",
                            storage_uuid="minigrid_offpolicy_expert",
                            loss_names=["offpolicy_expert_ce_loss"],
                            training_settings=TrainingSettings(
                                update_repeats=num_mini_batch * update_repeats,
                                num_mini_batch=1,
                            )
                        )
                    ],
                ),
            ],
            # As we don't have any on-policy losses, we set the next
            # two values to zero to ensure we don't attempt to
            # compute gradients for on-policy rollouts:
            num_mini_batch=0,
            update_repeats=0,
            total_train_steps=total_train_steps,
        )
        # fmt: on


# %%
"""
You'll have noted that it is possible to combine on-policy and off-policy training in the same stage, even though here
we apply pure off-policy training.

## Training

We recommend using a machine with a CUDA-capable GPU for this experiment. In order to start training, we just need to
invoke

```bash
PYTHONPATH=. python allenact/main.py -b projects/tutorials minigrid_offpolicy_tutorial -m 8 -o <OUTPUT_PATH>
```

Note that with the `-m 8` option we limit to 8 the number of on-policy task sampling processes used between off-policy
updates.

If everything goes well, the training success should quickly reach values around 0.7-0.8 on GPU and converge to values
close to 1 if given sufficient time to train.

If running tensorboard, you'll notice a separate group of scalars named `train-offpolicy-losses` and 
 `train-offpolicy-misc` with losses, approximate "experiences per second" (i.e. the number of off-policy experiences/steps
 being used to update the model per second), and other tracked values in addition to the standard `train-onpolicy-*`
  used for on-policy training. In the `train-metrics` and `train-misc` sections you'll find the metrics 
  quantifying the performance of the agent throughout training and some other plots showing training details.
  *Note that the x-axis for these plots is different than for the `train-offpolicy-*` sections*. This
  is because these plots use the number of rollout steps as the x-axis (i.e. steps that the trained agent
  takes interactively) while the `train-offpolicy-*` plots uses the number of offpolicy "experiences" that have
  been shown to the agent.
  

A view of the training progress about 5 hours after starting on a CUDA-capable GPU should look similar to the below
(note that training reached >99% success after about 50 minutes).

![off-policy progress](https://ai2-prior-allenact-public-assets.s3.us-west-2.amazonaws.com/tutorials/minigrid-offpolicy/minigrid-offpolicy-tutorial-tb.png)
"""
