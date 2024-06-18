"""Script to generate and evaluate samples from a trained diffusion model.

Usage
```bash
# requires config for loading model and data
# and checkpoint for loading model weights
TODO
```
"""
import argparse
import datetime
import os
import os.path as osp

import numpy as np
import pandas as pd
import torch
import wandb

# from lightning import seed_everything
from loguru import logger as log
from torch_geometric.data import Batch, Data
from tqdm import tqdm

from etflow.commons import load_pkl, save_pkl
from etflow.models import BaseFlow
from etflow.utils import instantiate_dataset, instantiate_model, read_yaml

torch.set_float32_matmul_precision("high")

DATA_FRAME_PATH = "/nfs/scratch/students/data/geom/preprocessed/geom_df_wo_filter.csv"


def get_datatime():
    return datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def cuda_available():
    return torch.cuda.is_available()


def main(
    config: str,
    checkpoint_path: str,
    indices: np.ndarray,
    counts: np.ndarray,
    nsteps: int,
    batch_size: int,
    eps: float,
    debug: bool,
    subset_type: str,
):
    # seed = config.get("seed", 42)
    # seed_everything(seed)

    if cuda_available():
        log.info("CUDA is available. Using GPU for sampling.")
        device = torch.device("cuda")
    else:
        log.warning("CUDA is not available. Using CPU for sampling.")
        device = torch.device("cpu")

    # instantiate datamodule and model
    dataset = instantiate_dataset(
        config["datamodule_args"]["dataset"], config["datamodule_args"]["dataset_args"]
    )

    model_type = config["model"]
    model = instantiate_model(config["model"], config["model_args"])

    # load model weights
    log.info(f"Loading model weights from {checkpoint_path}")
    state_dict = torch.load(checkpoint_path, map_location="cpu")["state_dict"]
    model.load_state_dict(state_dict)

    # move to device
    model: BaseFlow = model.to(device)
    model.eval()

    # max batch size
    max_batch_size = batch_size

    # load indices
    for i, idx in tqdm(enumerate(indices), total=len(indices)):
        data = dataset[idx]

        # get data for batch_size
        smiles = data.smiles
        log.info(f"Generating conformers for molecule: {smiles}")

        # calculate number of samples to generated
        count = counts[i]
        num_samples = 2 * count

        # we would want (num_samples, num_nodes, 3)
        generated_positions = []

        for batch_start in range(0, num_samples, max_batch_size):
            # get batch_size
            batch_size = min(max_batch_size, num_samples - batch_start)

            # batch the data
            batched_data = Batch.from_data_list([data] * batch_size)

            # get one_hot, edge_index, batch
            z, edge_index, batch, node_attr = (
                batched_data["atomic_numbers"].to(device),
                batched_data["edge_index"].to(device),
                batched_data["batch"].to(device),
                batched_data["node_attr"].to(device),
            )

            chiral_index = batched_data["chiral_index"].to(device)
            chiral_nbr_index = batched_data["chiral_nbr_index"].to(device)
            chiral_tag = batched_data["chiral_tag"].to(device)

            with torch.no_grad():
                if model_type == "BaseSFM":
                    # generate samples
                    pos = model.sample(
                        z,
                        edge_index,
                        batch,
                        node_attr=node_attr,
                        n_timesteps=nsteps,
                        eps=eps,
                        use_sde=True,
                    )
                else:
                    # generate samples
                    pos = model.sample(
                        z,
                        edge_index,
                        batch,
                        node_attr=node_attr,
                        n_timesteps=nsteps,
                        chiral_index=chiral_index,
                        chiral_nbr_index=chiral_nbr_index,
                        chiral_tag=chiral_tag,
                        eps=eps,
                    )

            # reshape to (num_samples, num_atoms, 3) using batch
            pos = pos.view(batch_size, -1, 3).cpu().detach().numpy()

            # append to generated_positions
            generated_positions.append(pos)

            # if debug mode, break after first batch
            if debug:
                break

        # if debug mode, break after first molecule
        if debug:
            break

        # concatenate generated_positions
        generated_positions = np.concatenate(
            generated_positions, axis=0
        )  # (num_samples, num_atoms, 3)

        # save to file
        if not debug:
            path = osp.join(output_dir, f"{idx}.pkl")
            log.info(
                f"Saving generated positions to file for smiles {smiles} at {path}"
            )
            save_pkl(path, generated_positions)

    # compile all generate pkl into a single file
    log.info("Compile all generated pickle files into a single file")
    df = pd.read_csv(DATA_FRAME_PATH)
    l = set([int(x.split(".pkl")[0]) for x in os.listdir(output_dir)])
    test_smiles = set([dataset[idx].smiles for idx in l])
    log.info(f"Number of generated files: {len(l)}")
    df_sub = df[(df.subset == subset_type) & (df.name.isin(test_smiles))].reset_index()
    df_sub["pos_ref"] = df_sub.apply(
        lambda row: dataset[row["index"]].pos.unsqueeze(0).numpy(), axis=1
    )

    # create pos_gen
    data_list = {}
    for index in tqdm(l):
        item = dataset[index]
        smiles = item.smiles

        pos_ref = np.concatenate(
            df_sub[df_sub["name"] == smiles]["pos_ref"].values.tolist()
        )
        pos_gen = load_pkl(f"{output_dir}/{index}.pkl")

        data_list[smiles] = Data(
            smiles=smiles, pos_ref=pos_ref, rdmol=item.mol, pos_gen=pos_gen
        )

    save_pkl(os.path.join(output_dir, "generated_files.pkl"), list(data_list.values()))


if __name__ == "__main__":
    # argparse checkpoint path
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", "-c", type=str, required=True)
    parser.add_argument("--checkpoint", "-k", type=str, required=True)
    parser.add_argument("--count_indices", "-i", type=str, required=True)
    parser.add_argument("--output_dir", "-o", type=str, required=False, default="logs/")
    parser.add_argument(
        "--dataset_type", "-t", type=str, required=False, default="drugs"
    )
    parser.add_argument("--batch_size", "-b", type=int, required=False, default=32)
    parser.add_argument("--nsteps", "-n", type=int, required=False, default=100)
    parser.add_argument(
        "--eps",
        "-e",
        type=float,
        default=1.0,
        required=False,
        help="Noise coefficient for SFM",
    )
    parser.add_argument("--debug", "-d", action="store_true")

    args = parser.parse_args()

    # debug mode
    debug = args.debug
    log.info(f"Debug mode: {debug}")

    # read config
    config_path = args.config
    assert osp.exists(config_path), "Config path does not exist."
    log.info(f"Loading config from: {config_path}")
    config = read_yaml(config_path)
    task_name = config.get("task_name", "default")

    # start wandb
    if not debug:
        log.info("Starting wandb...")
        wandb.init(
            project="Energy-Aware-MCG",
            entity="doms-lab",
            name=f"Sample Generation: {task_name}",
        )

        # log experiment info
        log_dict = {
            "config": config_path,
            "checkpoint": args.checkpoint,
            "debug": debug,
        }

        wandb.log(log_dict)

    # get checkpoint path
    checkpoint_path = args.checkpoint
    assert osp.exists(checkpoint_path), "Checkpoint path does not exist."

    # setup output directory for storing samples
    output_dir = osp.join(
        args.output_dir,
        f"samples/{task_name}/{get_datatime()}/flow_nsteps_{args.nsteps}_eps_{args.eps}",
    )
    if not debug:
        os.makedirs(output_dir, exist_ok=True)

    # load count indices path, indices for what smiles to use
    count_indices_path = args.count_indices
    assert osp.exists(count_indices_path), "Count indices path does not exist."
    log.info(f"Loading count indices from: {count_indices_path}")
    indices, counts = np.load(count_indices_path)
    log.info(f"Will be generating samples for {len(indices)} counts.")

    main(
        config,
        checkpoint_path,
        indices,
        counts,
        nsteps=args.nsteps,
        batch_size=args.batch_size,
        eps=args.eps,
        debug=debug,
        subset_type=args.dataset_type,
    )
