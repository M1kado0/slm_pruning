import time
import torch
import argparse
from torch import nn, optim, profiler
from naive_gpt import layers, utils


def load_model(name: str,
               module: str,
               seq_length: int,
               batch_size: int):
    cuda_device = 'cuda'

    #
    if name.find('opt') != -1:
        if name == 'opt-1024':
            n_heads = 16
            d_model = 1024
            d_feedforward = 4096
        elif name == 'opt-2048':
            n_heads = 32
            d_model = 2048
            d_feedforward = 8192
        elif name == 'opt-2560':
            n_heads = 32
            d_model = 2560
            d_feedforward = 10240
        else:
            raise NotImplementedError

        #
        def loader():
            return torch.randn(
                [batch_size, seq_length, d_model],
                requires_grad=True, device=cuda_device
            )
        #
        if module == 'mha':
            model = layers.TransformerBlock(
                d_model=d_model, n_heads=n_heads,
                layernorm_fn=nn.LayerNorm(d_model),
                attention_fn=layers.VanillaAttention(
                    d_head=d_model // n_heads,
                    p_dropout=0.0
                ),
                feedforward_fn=nn.Identity(),
                attention_bias=True,
                pre_norm=True
            )
            return loader, model.to(cuda_device)
        elif module == 'ffn':
            model = nn.Sequential(
                layers.Feedforward(
                    d_model=d_model,
                    d_feedforward=d_feedforward,
                    activation=nn.ReLU(),
                    p_dropout=0.0
                )
            )
            return loader, model.to(cuda_device)
        elif module == 'both':
            model = layers.TransformerBlock(
                d_model=d_model, n_heads=n_heads,
                layernorm_fn=nn.LayerNorm(d_model),
                attention_fn=layers.VanillaAttention(
                    d_head=d_model // n_heads,
                    p_dropout=0.0
                ),
                feedforward_fn=layers.Feedforward(
                    d_model=d_model,
                    d_feedforward=d_feedforward,
                    activation=nn.ReLU(),
                    p_dropout=0.0
                ),
                attention_bias=True,
                pre_norm=True
            )
            return loader, model.to(cuda_device)
        else:
            raise NotImplementedError

    elif name.find('llama') != -1:
        #added tinyllama
        if name == 'tinyllama-1b':
            n_heads = 32
            d_model = 2048
            d_feedforward = 5632
        elif name == 'llama-2560':
            n_heads = 20
            d_model = 2560
            d_feedforward = 6912
        elif name == 'llama-4096':
            n_heads = 32
            d_model = 4096
            d_feedforward = 11008
        elif name == 'llama-5120':
            n_heads = 32
            d_model = 5120
            d_feedforward = 13824
        else:
            raise NotImplementedError

        #
        def loader():
            return torch.randn(
                [batch_size, seq_length, d_model],
                requires_grad=True, device=cuda_device
            )
        #
        if module == 'mha':
            model = layers.TransformerBlock(
                d_model=d_model, n_heads=n_heads,
                layernorm_fn=layers.LlamaRMSNorm(d_model),
                attention_fn=layers.RotaryAttention(
                    d_head=d_model // n_heads,
                    p_dropout=0.0
                ),
                feedforward_fn=nn.Identity(),
                attention_bias=False,
                pre_norm=True
            )
            return loader, model.to(cuda_device)
        elif module == 'ffn':
            model = nn.Sequential(
                layers.LLaMaFeedforward(
                    d_model=d_model,
                    d_feedforward=d_feedforward,
                    activation=nn.SiLU()
                )
            )
            return loader, model.to(cuda_device)
        elif module == 'both':
            model = layers.TransformerBlock(
                d_model=d_model, n_heads=n_heads,
                layernorm_fn=layers.LlamaRMSNorm(d_model),
                attention_fn=layers.RotaryAttention(
                    d_head=d_model // n_heads,
                    p_dropout=0.0
                ),
                feedforward_fn=layers.LLaMaFeedforward(
                    d_model=d_model,
                    d_feedforward=d_feedforward,
                    activation=nn.SiLU()
                ),
                attention_bias=False,
                pre_norm=True
            )
            return loader, model.to(cuda_device)
        else:
            raise NotImplementedError

    else:
        raise NotImplementedError


def profile(name: str,
            tuning: str,
            module: str,
            seq_length: int,
            batch_size: int,
            backward: bool,
            compile: bool,
            d_lora: int):
    #
    print('name:', name)
    print('tuning:', tuning)
    print('module:', module)
    print('seq_length:', seq_length)
    print('batch_size:', batch_size)
    print('backward:', backward)
    print('compile:', compile)

    # model
    loader, model = load_model(
        name, module, seq_length, batch_size=batch_size
    )
    # upgrade
    if tuning == 'full':
        pass
    elif tuning == 'lora':
        upgrader = utils.ModuleUpgrader(
            handler=utils.LoRAHandler(
                d_lora=d_lora
            )
        )
        model = upgrader.visit(model)
    elif tuning == 'sparse':
        for stage in ['lora', 'ffn', 'mha_v1', 'mha_v2']:
            upgrader = utils.ModuleUpgrader(
                handler=utils.SparseLoRAHandler(
                    d_lora=d_lora, stage=stage
                )
            )
            model = upgrader.visit(model)
    else:
        raise RuntimeError
    device = loader().device
    model = model.to(device)
    optimizer = optim.AdamW(
        model.parameters(), lr=1e-4, weight_decay=1e-2
    )

    # compile
    if compile:
        model = torch.compile(model)

    # warm up
    for _ in range(20):
        torch.cuda.synchronize()
        x = loader()
        y_1 = model(x)
        if not backward:
            continue
        torch.sum(y_1).backward()
        optimizer.step()
        model.zero_grad()

    # simple
    time.sleep(2.0)
    torch.cuda.synchronize()
    before = time.time()
    x = loader()
    y_1 = model(x)
    if backward:
        torch.sum(y_1).backward()
    optimizer.step()
    model.zero_grad()
    torch.cuda.synchronize()
    print('simple timing: {:.2f}ms'.format(
        1000.0 * (time.time() - before)
    ))

    # profile
    time.sleep(2.0)
    with profiler.profile(
            activities=[
                profiler.ProfilerActivity.CUDA
            ],
            record_shapes=True,
            profile_memory=True,
            with_modules=True
    ) as prof:
        for _ in range(20):
            torch.cuda.synchronize()
            x = loader()
            y_1 = model(x)
            if not backward:
                continue
            torch.sum(y_1).backward()
            optimizer.step()
            model.zero_grad()
    print(
        prof.key_averages().table(
            sort_by='cuda_time_total', row_limit=5
        )
    )

    # memory
    print(torch.cuda.memory_summary())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--name', default='opt-2048',
        help='specify model name or path'
    )
    parser.add_argument(
        '--tuning', default='sparse',
        help='specify full, lora, or sparse'
    )
    parser.add_argument(
        '--module', default='both',
        help='specify module in mha, ffn, or both'
    )
    parser.add_argument(
        '--backward', action='store_true',
        help='specify to enable backard'
    )
    parser.add_argument(
        '--compile', action='store_true',
        help='specify to enable torch.compile'
    )
    parser.add_argument(
        '--seq_length', default=512, type=int,
        help='specify sequence length'
    )
    parser.add_argument(
        '--batch_size', default=16, type=int,
        help='specify batch size'
    )
    parser.add_argument(
        '--d_lora', help='dim oflow rank adaptation',
        default=16
    )
    args = parser.parse_args()

    #
    profile(
        name=args.name,
        tuning=args.tuning,
        module=args.module,
        seq_length=args.seq_length,
        batch_size=args.batch_size,
        backward=args.backward,
        compile=args.compile,
        d_lora=args.d_lora
    )


if __name__ == '__main__':
    main()
