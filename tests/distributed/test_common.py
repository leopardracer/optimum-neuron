# coding=utf-8
# Copyright 2023 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""General tests related to distributed training."""

from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Dict

import pytest
import safetensors
import torch
from peft import PeftModelForCausalLM
from transformers import LlamaForCausalLM

from optimum.neuron.accelerate.optimizer import NeuronAcceleratedOptimizer
from optimum.neuron.accelerate.utils.dataclasses import NeuronDistributedType
from optimum.neuron.distributed.checkpointing import consolidate_model_parallel_checkpoints_to_unified_checkpoint
from optimum.neuron.distributed.utils import (
    MODEL_PARALLEL_SHARDS_DIR_NAME,
    make_optimizer_constructor_lazy,
)
from optimum.neuron.models.training import LlamaForCausalLM as NeuronLlamaForCausalLM
from optimum.neuron.models.training.config import TrainingNeuronConfig
from optimum.neuron.peft import NeuronPeftModelForCausalLM
from optimum.neuron.utils.import_utils import (
    is_neuronx_distributed_available,
    is_torch_xla_available,
)
from optimum.neuron.utils.testing_utils import is_trainium_test

from .. import DistributedTest
from ..utils import StaticSeedPatcher, create_accelerator, get_model, get_model_inputs


if is_torch_xla_available():
    import torch_xla.core.xla_model as xm
    import torch_xla.runtime as xr

if is_neuronx_distributed_available():
    from neuronx_distributed.modules.qkv_linear import GQAQKVColumnParallelLinear
    from neuronx_distributed.parallel_layers.parallel_state import (
        get_data_parallel_rank,
        get_pipeline_model_parallel_rank,
        get_tensor_model_parallel_group,
        get_tensor_model_parallel_rank,
    )
    from neuronx_distributed.parallel_layers.utils import move_all_tensor_to_cpu
    from neuronx_distributed.pipeline import NxDPPModel
    from neuronx_distributed.utils.model_utils import move_model_to_device

if TYPE_CHECKING:
    from transformers import PreTrainedModel

MODEL_NAME = "michaelbenayoun/llama-2-tiny-16layers-random"
MODEL_NAME_WITH_4_KV_HEADS = "michaelbenayoun/llama-2-tiny-4kv-heads-4layers-random"


def get_tiny_llama_model(
    tp_size: int = 1,
    pp_size: int = 1,
    lazy_load: bool = False,
    from_config: bool = False,
    use_static_seed_patcher: bool = False,
    add_random_noise: bool = False,
) -> "PreTrainedModel":
    return get_model(
        LlamaForCausalLM,
        MODEL_NAME,
        tp_size=tp_size,
        pp_size=pp_size,
        lazy_load=lazy_load,
        from_config=from_config,
        use_static_seed_patcher=use_static_seed_patcher,
        add_random_noise=add_random_noise,
    )


def get_optimizer(model: torch.nn.Module, lazy: bool = False, with_groups: bool = True) -> torch.optim.Optimizer:
    adam_cls = torch.optim.AdamW
    if lazy:
        adam_cls = make_optimizer_constructor_lazy(adam_cls)

    if with_groups:
        groups = [
            {"params": (p for idx, p in enumerate(model.parameters()) if idx % 2 == 0), "lr": 1e-2},
            {"params": (p for idx, p in enumerate(model.parameters()) if idx % 2 == 1), "lr": 1e-6},
        ]
    else:
        groups = model.parameters()

    return adam_cls(groups)


def move_params_to_cpu(parameters):
    parameters = list(parameters)
    xm.mark_step()
    # `move_all_tensor_to_cpu` only selects `torch.Tensor`, so we need to move the parameters' data.
    cpu_params = move_all_tensor_to_cpu([p.data for p in parameters])
    return cpu_params


@is_trainium_test
class TestCommonDistributed(DistributedTest):
    # TODO: enable dp=4,tp=pp=2 when working on the multi-node training PR.
    # Anything related to PP seems broken because of the way the model is traced.
    @pytest.fixture(
        scope="class",
        params=[[2, 1, 1], [2, 2, 1]],  # , [2, 1, 2]],
        ids=["dp=2", "tp=2"],  # "pp=2"],
    )
    def parallel_sizes(self, request):
        return request.param

    @pytest.fixture(scope="class", params=[False, True], ids=["regular_load", "lazy_load"])
    def lazy_load(self, request):
        return request.param

    @pytest.fixture(scope="class", params=[False, True], ids=["from_pretrained", "from_config"])
    def from_config(self, request):
        return request.param

    @pytest.fixture(scope="class", params=[False, True], ids=["regular_optimizer", "lazy_optimizer"])
    def lazy_optimizer(self, request):
        return request.param

    @pytest.fixture(scope="class", params=[False, True], ids=["without_groups", "with_groups"])
    def with_groups(self, request):
        return request.param

    @pytest.fixture(scope="class", params=[False, True], ids=["without_zero_1", "with_zero_1"])
    def zero_1(self, request):
        return request.param

    @pytest.fixture(scope="class", params=[1, 12], ids=["without_grad_acc", "with_grad_acc=12"])
    def gradient_accumulation_steps(self, request):
        return request.param

    @pytest.fixture(scope="class", params=[None, 0.01], ids=["without_clip_grad_norm", "with_clip_grad_norm"])
    def max_grad_norm(self, request):
        return request.param

    @pytest.fixture(scope="class", params=[False, True], ids=["xser_disabled", "xser_enabled"])
    def use_xser(self, request):
        return request.param

    def test_optimizer_parameters_match_model_parameters(
        self, lazy_load, lazy_optimizer, with_groups, zero_1, parallel_sizes
    ):
        num_workers, tp_size, pp_size = parallel_sizes
        dp_size = num_workers // (tp_size * pp_size)
        if dp_size == 1 and zero_1:
            pytest.skip("zero_1 needs to be tested only for dp_size > 1")

        model = get_tiny_llama_model(tp_size=tp_size, pp_size=pp_size, lazy_load=lazy_load)
        optimizer = get_optimizer(model, lazy_optimizer, with_groups)

        accelerator = create_accelerator(tp_size, pp_size, zero_1=zero_1)
        if tp_size > 1 or pp_size > 1:
            assert accelerator.state.distributed_type is NeuronDistributedType.MODEL_PARALLELISM

        model = accelerator.prepare(model)

        # Under DDP only setting, the optimizer needs to be created after the model has been moved.
        if tp_size == 1 and pp_size == 1:
            optimizer = get_optimizer(model, lazy_optimizer, with_groups)

        optimizer = accelerator.prepare(optimizer)

        assert isinstance(optimizer, NeuronAcceleratedOptimizer)

        if isinstance(model, NxDPPModel):
            model_parameters = set(model.local_parameters())
        else:
            model_parameters = set(model.parameters())
        optimizer_parameters = {p for group in optimizer.param_groups for p in group["params"]}

        assert model_parameters == optimizer_parameters

    def test_optimizer_step(self, zero_1, gradient_accumulation_steps, max_grad_norm, parallel_sizes):
        num_workers, tp_size, pp_size = parallel_sizes
        dp_size = num_workers // (tp_size * pp_size)
        if dp_size == 1 and zero_1:
            pytest.skip("zero_1 needs to be tested only for dp_size > 1")

        # TODO: investigate that with the AWS team to find a solution.
        if dp_size > 1 and zero_1 and max_grad_norm is not None:
            pytest.skip("Gradient clipping seems to not work properly with ZeRO-1.")

        model = get_tiny_llama_model(tp_size=tp_size, pp_size=pp_size, use_static_seed_patcher=True)

        if tp_size == pp_size == 1:
            move_model_to_device(model, xm.xla_device())

        optimizer = get_optimizer(model, with_groups=False)

        accelerator = create_accelerator(
            tp_size, pp_size, zero_1=zero_1, gradient_accumulation_steps=gradient_accumulation_steps
        )

        model, optimizer = accelerator.prepare(model, optimizer)
        assert isinstance(optimizer, NeuronAcceleratedOptimizer)

        inputs = get_model_inputs(model, MODEL_NAME)

        def move_grads_to_cpu(parameters):
            grads = [p.grad for p in parameters]
            grads = move_all_tensor_to_cpu(grads)
            return grads

        if pp_size == 1:
            inputs = {k: v.to(xm.xla_device()) for k, v in inputs.items()}

        current_parameters = move_params_to_cpu(
            model.local_parameters() if isinstance(model, NxDPPModel) else model.parameters()
        )

        for step in range(int(1.5 * gradient_accumulation_steps)):
            is_optimizer_update_step = (step + 1) % gradient_accumulation_steps == 0
            with accelerator.accumulate(model):
                if pp_size > 1:
                    orig_parameters = current_parameters
                    loss = model.run_train(**inputs)
                    xm.mark_step()

                    if max_grad_norm is not None:
                        accelerator.clip_grad_norm_(
                            model.local_parameters(),
                            max_norm=max_grad_norm,
                            norm_type=2,
                            postpone_clipping_to_optimizer_step=True,
                        )

                    # Checking that at least some of the parameters have a gradient.
                    grads_on_cpu = move_grads_to_cpu(model.local_parameters())
                    assert any(torch.all(grad != 0) for grad in grads_on_cpu)

                    optimizer.step()

                    # Checking only after an actual optimizer step that the norm has been clipped because it happens
                    # during the optimizer step in some cases.
                    if is_optimizer_update_step and max_grad_norm is not None:
                        grads_on_cpu = move_grads_to_cpu(model.local_parameters())
                        norms = [torch.linalg.vector_norm(grad, 2) for grad in grads_on_cpu]
                        total_norm = torch.linalg.vector_norm(torch.stack(norms), 2)
                        assert total_norm <= max_grad_norm

                    optimizer.zero_grad()

                    grads_on_cpu = move_grads_to_cpu(model.local_parameters())
                    if is_optimizer_update_step:
                        # At this point, no parameter should have a gradient.
                        assert all(grad is None or torch.all(grad == 0) for grad in grads_on_cpu)

                    current_parameters = move_params_to_cpu(model.local_parameters())
                else:
                    orig_parameters = current_parameters
                    outputs = model(**inputs)
                    loss = outputs["loss"]
                    xm.mark_step()
                    loss.backward()

                    if max_grad_norm is not None:
                        accelerator.clip_grad_norm_(
                            model.parameters(),
                            max_norm=max_grad_norm,
                            norm_type=2,
                            postpone_clipping_to_optimizer_step=True,
                        )

                    # Checking that at least some of the parameters have a gradient.
                    grads_on_cpu = move_grads_to_cpu(model.parameters())
                    assert any(torch.all(grad != 0) for grad in grads_on_cpu)

                    optimizer.step()

                    # Checking only after an actual optimizer step that the norm has been clipped because it happens
                    # during the optimizer step in some cases.
                    if is_optimizer_update_step and max_grad_norm is not None:
                        grads_on_cpu = move_grads_to_cpu(model.parameters())
                        norms = [torch.linalg.vector_norm(grad, 2) for grad in grads_on_cpu]
                        total_norm = torch.linalg.vector_norm(torch.stack(norms), 2)
                        assert total_norm <= max_grad_norm

                    optimizer.zero_grad()

                    # At this point, no parameter should have a gradient.
                    if is_optimizer_update_step:
                        grads_on_cpu = move_grads_to_cpu(model.parameters())
                        assert all(grad is None or torch.all(grad == 0) for grad in grads_on_cpu)

                    current_parameters = move_params_to_cpu(model.parameters())

                if is_optimizer_update_step:
                    assert any(torch.any(p1 != p2) for (p1, p2) in zip(orig_parameters, current_parameters))
                else:
                    assert all(torch.all(p1 == p2) for (p1, p2) in zip(orig_parameters, current_parameters))

    def test_lazy_load(self, from_config, parallel_sizes):
        _, tp_size, pp_size = parallel_sizes

        if from_config and (tp_size > 1 or pp_size > 1):
            pytest.skip("It is not easy to compare parameters value in this case because of initialization.")

        model = get_tiny_llama_model(
            tp_size=1, pp_size=1, lazy_load=False, from_config=from_config, use_static_seed_patcher=True
        )

        orig_parameters: Dict[str, torch.nn.Parameter] = dict(model.named_parameters())

        accelerator = create_accelerator(tp_size, pp_size)
        lazy_model = get_tiny_llama_model(
            tp_size=tp_size, pp_size=pp_size, lazy_load=True, from_config=from_config, use_static_seed_patcher=True
        )
        static_seed_patcher = StaticSeedPatcher(42)
        with static_seed_patcher:
            lazy_model = accelerator.prepare(lazy_model)

        if pp_size > 1:
            named_parameters = dict(lazy_model.local_named_parameters())
        else:
            named_parameters = dict(lazy_model.named_parameters())

        xm.mark_step()

        for name, param in named_parameters.items():
            orig = orig_parameters[name]
            if orig.shape != param.shape:
                if orig.dim() == 1:
                    gather_dim = 0
                elif orig.dim() == 2:
                    gather_dim = 1 if orig.shape[0] == param.shape[0] else 0
                else:
                    raise ValueError(f"The case where the weight as a rank of {orig.dim()} is not supported.")
                gathered = [torch.empty(param.shape) for _ in range(tp_size)]
                torch.distributed.all_gather(gathered, param, group=get_tensor_model_parallel_group())
                gathered_param = torch.cat(gathered, dim=gather_dim)
            else:
                gathered_param = param

            orig = orig.to("cpu")
            gathered_param = gathered_param.to("cpu")
            xm.mark_step()

            print(f"Comparing parameter named {name}")
            torch.testing.assert_close(orig, gathered_param)

    def test_save_model_and_load_model(self, parallel_sizes, tmpdir, monkeypatch):
        _, tp_size, pp_size = parallel_sizes
        dp_rank = get_data_parallel_rank()
        tp_rank = get_tensor_model_parallel_rank()
        pp_rank = get_pipeline_model_parallel_rank()

        tmpdir = Path(tmpdir)

        model = get_tiny_llama_model(tp_size=tp_size, pp_size=pp_size, lazy_load=False, add_random_noise=True)

        accelerator = create_accelerator(tp_size, pp_size)
        model = accelerator.prepare(model)
        accelerator.save_state(tmpdir.as_posix())
        accelerator.state._reset_state(reset_partial_state=True)
        del accelerator

        xm.rendezvous("wait_after_save")

        if pp_size > 1:
            # We need to disable `NxDPPModel._set_distributed` since it is already done during the creation of the
            # first model, otherwise creating new `NxDPPModel`s will fail.
            monkeypatch.setattr(NxDPPModel, "_set_distributed", lambda _: _)

        tmpdir_content = [path.name for path in tmpdir.glob("**/*")]
        pytorch_checkpoint_exists = "pytorch_model.bin" in tmpdir_content
        safetensors_checkpoint_exists = "model.safetensors" in tmpdir_content

        if tp_size > 1 or pp_size > 1:
            if dp_rank == 0:
                stem = f"dp_rank_{dp_rank:02d}_tp_rank_{tp_rank:02d}_pp_rank_{pp_rank:02d}"
                ref_data_file_name = f"{stem}.pt"
                tensors_directory = f"{stem}.pt.tensors"
                assert not pytorch_checkpoint_exists
                assert not safetensors_checkpoint_exists
                assert MODEL_PARALLEL_SHARDS_DIR_NAME in tmpdir_content
                assert "model" in tmpdir_content
                assert ref_data_file_name in tmpdir_content
                assert tensors_directory in tmpdir_content
        else:
            assert pytorch_checkpoint_exists or safetensors_checkpoint_exists

        # Making sure that we end-up with a different model when starting over.
        new_model = get_tiny_llama_model(tp_size=tp_size, pp_size=pp_size, lazy_load=False, add_random_noise=True)
        new_accelerator = create_accelerator(tp_size, pp_size)
        new_model = new_accelerator.prepare(new_model)
        new_accelerator.state._reset_state(reset_partial_state=True)
        del new_accelerator

        if pp_size == 1:
            model_parameters = move_params_to_cpu(model.parameters())
            new_model_parameters = move_params_to_cpu(new_model.parameters())
        else:
            model_parameters = move_params_to_cpu(model.local_parameters())
            new_model_parameters = move_params_to_cpu(new_model.local_parameters())

        assert any(
            torch.all(p1 == 0.0) or torch.all(p1 == 1.0) or torch.all(p1 != p2)
            for p1, p2 in zip(model_parameters, new_model_parameters)
        )

        # Checking that when providing a checkpoint, we end-up with the same model as the original.
        new_model = get_tiny_llama_model(tp_size=tp_size, pp_size=pp_size, lazy_load=False, add_random_noise=True)
        new_accelerator = create_accelerator(tp_size, pp_size, checkpoint_dir=tmpdir)
        new_model = new_accelerator.prepare(new_model)

        # If there is no model parallelism, the checkpoint weights will not be loaded automatically since we do not
        # call parallelize, so we do it manually.
        if tp_size == pp_size == 1:
            if pytorch_checkpoint_exists:
                filename = "pytorch_model.bin"
                checkpoint_path = tmpdir / filename
                new_model.load_state_dict(torch.load(checkpoint_path))
            else:
                filename = "model.safetensors"
                checkpoint_path = tmpdir / filename
                new_model.load_state_dict(safetensors.torch.load_file(checkpoint_path))

        if pp_size == 1:
            model_parameters = move_params_to_cpu(model.parameters())
            new_model_parameters = move_params_to_cpu(new_model.parameters())
        else:
            model_parameters = move_params_to_cpu(model.local_parameters())
            new_model_parameters = move_params_to_cpu(new_model.local_parameters())

        if dp_rank == 0:
            assert all(torch.all(p1 == p2) for p1, p2 in zip(model_parameters, new_model_parameters))

    @pytest.mark.parametrize(
        "world_size,tp_size,pp_size,kv_size_multiplier,model_name",
        [
            [8, 2, 1, None, MODEL_NAME_WITH_4_KV_HEADS],
            # [8, 1, 2, None, MODEL_NAME_WITH_4_KV_HEADS],
            # [16, 2, 2, None, MODEL_NAME_WITH_4_KV_HEADS],
            # [16, 8, 2, None, MODEL_NAME_WITH_4_KV_HEADS],
            # [16, 8, 2, 4, MODEL_NAME_WITH_4_KV_HEADS],
        ],
        ids=[
            "tp=2",
            # "pp=2",
            # "dp=4,tp=pp=2",
            # "dp=1,tp=8,pp=2,kv_size_multiplier=None,GQAQKVColumnParallelLinear",
            # "dp=1,tp=8,pp=2,kv_size_multiplier=4,GQAQKVColumnParallelLinear",
        ],
    )
    def test_consolidate_model_parallel_checkpoints(
        self,
        tmpdir,
        world_size,
        tp_size,
        pp_size,
        kv_size_multiplier,
        model_name,
        use_xser,
    ):
        orig_model = get_model(
            LlamaForCausalLM,
            model_name,
            use_static_seed_patcher=True,
        )
        orig_model_path = Path(tmpdir) / "orig_model"
        if xr.global_ordinal() == 0:
            # Saving to pytorch instead of safetensors because it fails otherwise for pickling issues with distributed tests.
            orig_model.save_pretrained(orig_model_path, safe_serialization=False)

        accelerator = create_accelerator(tp_size, pp_size, kv_size_multiplier=kv_size_multiplier, use_xser=use_xser)
        _ = accelerator.prepare(orig_model)

        output_dir = Path(tmpdir) / "parallel_model"
        accelerator.save_state(output_dir.as_posix())

        xm.rendezvous("Saving done.")

        consolidation_dir = Path(tmpdir) / "consolidated"
        if xr.global_ordinal() == 0:
            consolidate_model_parallel_checkpoints_to_unified_checkpoint(
                output_dir, consolidation_dir, save_format="pytorch"
            )
            consolidated_state_dict = torch.load(consolidation_dir / "pytorch_model.bin")
            orig_state_dict = torch.load(orig_model_path / "pytorch_model.bin")

            assert orig_state_dict.keys() == consolidated_state_dict.keys()
            for key in orig_state_dict:
                orig_tensor = orig_state_dict[key]
                consolidated_tensor = consolidated_state_dict[key]
                print(f"Testing that {key} match")
                torch.testing.assert_close(orig_tensor, consolidated_tensor)

    @pytest.mark.parametrize(
        "world_size,tp_size,pp_size,kv_size_multiplier,fuse_qkv",
        [
            [8, 2, 1, None, False],
            [8, 8, 1, None, False],
            [8, 8, 1, 4, False],
            [8, 8, 1, 4, True],
        ],
        ids=[
            "dp=4,tp=2,pp=1",
            "dp=1,tp=8,pp=1,kv_size_multiplier=None,GQAQKVColumnParallelLinear",
            "dp=1,tp=8,pp=1,kv_size_multiplier=4,GQAQKVColumnParallelLinear",
            "dp=1,tp=8,pp=1,kv_size_multiplier=4,GQAQKVColumnParallelLinear,fuse_qkv",
        ],
    )
    def test_consolidate_custom_model_parallel_checkpoints(
        self, tmpdir, world_size, tp_size, pp_size, kv_size_multiplier, fuse_qkv, use_xser
    ):
        tmpdir = Path(tmpdir)
        orig_model = LlamaForCausalLM.from_pretrained(MODEL_NAME_WITH_4_KV_HEADS)

        if xr.global_ordinal() == 0:
            orig_model.save_pretrained(tmpdir / "orig_model", safe_serialization=False)

        trn_config = TrainingNeuronConfig(
            tensor_parallel_size=tp_size,
            pipeline_parallel_size=pp_size,
            use_xser=use_xser,
            async_save=False,
            fuse_qkv=fuse_qkv,
        )
        custom_model = NeuronLlamaForCausalLM.from_pretrained(MODEL_NAME_WITH_4_KV_HEADS, trn_config)

        custom_model.save_pretrained(tmpdir / "custom_model")

        xm.rendezvous("Saving done.")

        if xr.global_ordinal() == 0:
            consolidate_model_parallel_checkpoints_to_unified_checkpoint(
                tmpdir / "custom_model",
                tmpdir / "consolidated_model",
                save_format="pytorch",
            )
            orig_state_dict = torch.load(tmpdir / "orig_model" / "pytorch_model.bin", weights_only=True)
            consolidated_state_dict = torch.load(
                tmpdir / "consolidated_model" / "pytorch_model.bin", weights_only=True
            )

            assert orig_state_dict.keys() == consolidated_state_dict.keys(), (
                "Keys of the original state dict and consolidated state dict do not match."
            )
            for key in orig_state_dict:
                orig_tensor = orig_state_dict[key]
                consolidated_tensor = consolidated_state_dict[key]
                print(f"Testing that {key} match")
                torch.testing.assert_close(orig_tensor, consolidated_tensor)

    @pytest.mark.parametrize(
        "world_size,tp_size,pp_size,kv_size_multiplier,fuse_qkv",
        [
            [8, 2, 1, None, False],
            [8, 8, 1, 4, False],
            [8, 8, 1, 4, True],
        ],
        ids=[
            "dp=4,tp=2,pp=1",
            "dp=1,tp=8,pp=1,kv_size_multiplier=4,GQAQKVColumnParallelLinear",
            "dp=1,tp=8,pp=1,kv_size_multiplier=4,GQAQKVColumnParallelLinear,fuse_qkv",
        ],
    )
    def test_consolidate_custom_lora_model_parallel_checkpoints(
        self, tmpdir, world_size, tp_size, pp_size, kv_size_multiplier, fuse_qkv, use_xser
    ):
        tmpdir = Path(tmpdir)
        orig_model = LlamaForCausalLM.from_pretrained(MODEL_NAME_WITH_4_KV_HEADS)

        first_lora_adapter_model_name_or_path = (
            "michaelbenayoun/lora-qkv-included-llama-2-tiny-4kv-heads-4layers-random"
        )
        second_lora_adapter_model_name_or_path = (
            "michaelbenayoun/lora-2-qkv-included-llama-2-tiny-4kv-heads-4layers-random"
        )

        # Loading the LoRA adapters into the original model.
        orig_model = PeftModelForCausalLM.from_pretrained(
            orig_model,
            first_lora_adapter_model_name_or_path,
            adapter_name="default",
        )
        orig_model.load_adapter(
            second_lora_adapter_model_name_or_path,
            adapter_name="test",
        )

        if xr.global_ordinal() == 0:
            orig_model.save_pretrained(tmpdir / "orig_model", safe_serialization=False)

        trn_config = TrainingNeuronConfig(
            tensor_parallel_size=tp_size,
            pipeline_parallel_size=pp_size,
            use_xser=use_xser,
            async_save=False,
            fuse_qkv=fuse_qkv,
        )
        custom_model = NeuronLlamaForCausalLM.from_pretrained(MODEL_NAME_WITH_4_KV_HEADS, trn_config)

        # Loading the LoRA adapters into the custom model.
        custom_model = NeuronPeftModelForCausalLM.from_pretrained(
            custom_model,
            first_lora_adapter_model_name_or_path,
            adapter_name="default",
        )
        custom_model.load_adapter(
            second_lora_adapter_model_name_or_path,
            adapter_name="test",
        )

        has_gqa_qkv_column_parallel_linear = any(
            isinstance(m, GQAQKVColumnParallelLinear) for m in custom_model.modules()
        )

        # Some weights need to be averaged before comparing them.
        # For now it is only the case for the LoRA A weights when there is linear fusion involved.
        # We specify this in a dictionary where:
        #  - the key is the suffix of the weight that we want to average
        # - the value is a list of suffixes that we want to average with the key suffix.
        key_suffixes_of_weights_to_average = {
            "gate_proj.lora_A.weight": ["gate_proj.lora_A.weight", "up_proj.lora_A.weight"],
            "up_proj.lora_A.weight": ["gate_proj.lora_A.weight", "up_proj.lora_A.weight"],
        }
        if has_gqa_qkv_column_parallel_linear:
            key_suffixes_of_weights_to_average.update(
                {
                    "q_proj.lora_A.weight": ["q_proj.lora_A.weight", "k_proj.lora_A.weight", "v_proj.lora_A.weight"],
                    "k_proj.lora_A.weight": ["q_proj.lora_A.weight", "k_proj.lora_A.weight", "v_proj.lora_A.weight"],
                    "v_proj.lora_A.weight": ["q_proj.lora_A.weight", "k_proj.lora_A.weight", "v_proj.lora_A.weight"],
                }
            )

        custom_model.save_pretrained(tmpdir / "custom_model")
        xm.rendezvous("Saving done.")

        if xr.global_ordinal() == 0:
            consolidate_model_parallel_checkpoints_to_unified_checkpoint(
                tmpdir / "custom_model",
                tmpdir / "consolidated_model",
                save_format="pytorch",
            )
            for adapter_name in ["default", "test"]:
                if adapter_name == "default":
                    orig_state_dict = torch.load(tmpdir / "orig_model" / "adapter_model.bin", weights_only=True)
                    consolidated_state_dict = torch.load(
                        tmpdir / "consolidated_model" / "adapter_model.bin", weights_only=True
                    )
                else:
                    orig_state_dict = torch.load(
                        tmpdir / "orig_model" / adapter_name / "adapter_model.bin", weights_only=True
                    )
                    consolidated_state_dict = torch.load(
                        tmpdir / "consolidated_model" / adapter_name / "adapter_model.bin", weights_only=True
                    )

                assert orig_state_dict.keys() == consolidated_state_dict.keys(), (
                    f"Keys of the original state dict and consolidated state dict do not match for adapter {adapter_name}."
                )
                for key in orig_state_dict:
                    orig_tensor = orig_state_dict[key]
                    consolidated_tensor = consolidated_state_dict[key]
                    print(f"Testing that {key} match for adapter {adapter_name}")
                    if any(key.endswith(suffix) for suffix in key_suffixes_of_weights_to_average):
                        continue
                    else:
                        torch.testing.assert_close(orig_tensor, consolidated_tensor)

                # For the weights that need to be averaged before compared, we do it here.
                orig_tensors = defaultdict(list)
                for key_suffix, suffixes in key_suffixes_of_weights_to_average.items():
                    for key in orig_state_dict.keys():
                        # If the key ends with the key_suffix, it means that the associated weight needs to be averaged
                        # with weights that end with the suffixes.
                        if key.endswith(key_suffix):
                            # key_prefix is basically the fully qualified name of the module that contains the weight.
                            key_prefix = key[: -len(key_suffix)]
                            # We collect all the tensors that need to be averaged.
                            for name, tensor in orig_state_dict.items():
                                for suffix in suffixes:
                                    if name.endswith(suffix):
                                        # name_prefix is the fully qualified name of the module that contains the weight.
                                        name_prefix = name[: -len(suffix)]
                                        # We only keep the tensors that are from the same module as the key.
                                        if name_prefix == key_prefix:
                                            orig_tensors[key].append(tensor)

                for key, tensors in orig_tensors.items():
                    orig_tensor = torch.mean(torch.stack(tensors, dim=0), dim=0)
                    consolidated_tensor = consolidated_state_dict[key]
                    print(f"Testing that {key} match for adapter {adapter_name}")
                    torch.testing.assert_close(orig_tensor, consolidated_tensor)
