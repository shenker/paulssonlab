import re
from itertools import product
from numbers import Integral
from pathlib import Path

import cachetools
import h5py
import nd2reader
import numpy as np
import pandas as pd
import zarr
from cytoolz import excepts
from skimage.io import imread
from tqdm.auto import tqdm

from paulssonlab.image_analysis.image import pad
from paulssonlab.image_analysis.util import get_delayed
from paulssonlab.image_analysis.workflow import get_nd2_frame, get_nd2_reader
from paulssonlab.io.metadata import parse_nd2_metadata
from paulssonlab.util.core import first

ND2READER_CACHE = cachetools.LFUCache(maxsize=48)


def _select_indices(idxs, slice_):
    if slice_ is None:
        return idxs
    elif isinstance(slice_, slice):
        return idxs[slice_]
    elif isinstance(slice_, Integral):
        return idxs[:slice_]
    else:
        return slice_


def _numpy_to_py(obj):
    if hasattr(obj, "item"):
        return obj.item()
    else:
        return obj


# TODO: add random_axes argument for testing?
# random_axes="v", axis_order="vtcz" processes whole FOVs time-series in random FOV order
def send_nd2(filename, axis_order="tvcz", slices={}, delayed=True, send_metadata=True):
    delayed = get_delayed(delayed)
    nd2 = get_nd2_reader(filename)
    # TODO: allow slicing by idx/name?? e.g., c=[0] or channel=["RFP-EM", "YFP-EM"]
    iterators = [
        _select_indices(
            np.arange(nd2.sizes.get(axis_name, 1)), slices.get(axis_name, slice(None))
        )
        for axis_name in axis_order
    ]
    iterator = product(*iterators)
    # send whole-file metadata
    # TODO: we don't wrap in delayed, should we?
    if send_metadata:
        nd2_metadata = parse_nd2_metadata(nd2)
        yield {"type": "nd2_metadata", "metadata": nd2_metadata}
    # send frames
    for idxs in iterator:
        coords = dict(zip(axis_order, (_numpy_to_py(idx) for idx in idxs)))
        # TODO: this might not work delayed because ND2Reader doesn't reopen file handles correctly
        # TODO: use an LRU buffer for open ND2 file handles?
        # image = delayed(nd2.get_frame_2D)(**coords)
        fov_num = coords["v"]
        channel = nd2.metadata["channels"][coords["c"]]
        z_level = coords["z"]
        t = coords["t"]
        image = delayed(get_nd2_frame)(filename, fov_num, channel, t)
        # we can put arbitrary per-frame metadata here
        # TODO: do we need a way to encode metadata that differs between individual frames? [maybe not.]
        image_metadata = {
            "channel": channel,
            "z_level": z_level,
            "fov_num": fov_num,
            "t": t,
        }
        # TODO: we will replace the above with a more elegant way of encoding
        # channel, timepoint, FOV number, z-level, etc.
        msg = {"type": "image", "image": image, "metadata": image_metadata}
        yield msg


def _slice_directory_keys(keys, slices, values=None):
    # TODO: I don't like the name values here or axis_coords below
    # xarray calls a similar thing coords, but that may be confusing here
    if values is None:
        values = tuple(map(np.unique, zip(*keys)))
    selected_idxs = tuple(map(_select_indices, values, slices))
    for key in sorted(keys):
        for idx, selected_idx in zip(key, selected_idxs):
            if idx not in selected_idx:
                break
        else:
            yield key


def _iter_dir(root, recursive=False, files=True, directories=True):
    if recursive:
        for path, path_dirs, path_files in root.walk():
            if files:
                for file in path_files:
                    yield path / file
            if directories:
                for dir in path_dirs:
                    yield path / dir
    else:
        for file in root.iterdir():
            if files and file.is_file():
                yield file
            elif directories and file.is_dir():
                yield file


def slice_directory(
    root_dir,
    pattern,
    axis_order="tvc",
    slices={},
    recursive=False,
    files=True,
    directories=True,
):
    pattern = re.compile(pattern)
    root_dir = Path(root_dir)
    unmatched_filenames = []
    keys_to_filenames = {}
    for file in _iter_dir(
        root_dir, recursive=recursive, files=files, directories=directories
    ):
        match = pattern.match(str(file.relative_to(root_dir)))
        if match:
            idxs = {
                k: excepts(ValueError, int, lambda _: v)(v)
                for k, v in match.groupdict().items()
            }
            key = tuple([idxs[axis] for axis in axis_order])
            keys_to_filenames[key] = file
        else:
            unmatched_filenames.append(file)
    # here axis_coords is analogous to what xarray calls DataArray.coords
    axis_coords = tuple(map(np.unique, zip(*keys_to_filenames.keys())))
    selected_keys = _slice_directory_keys(
        keys_to_filenames.keys(),
        [slices.get(axis, slice(None)) for axis in axis_order],
        values=axis_coords,
    )
    iterator = ((key, root_dir / keys_to_filenames[key]) for key in selected_keys)
    axis_coords = dict(zip(axis_order, axis_coords))
    return iterator, unmatched_filenames, axis_coords


def send_eaton_fish(
    root_dir,
    pattern=r"fov=(?P<v>\d+)_config=(?P<c>\w+)_t=(?P<t>\d+)",
    axis_order="tvc",
    slices={},
    include_fixation=False,
    include_tables=False,
    delayed=True,
):
    root_dir = Path(root_dir)
    delayed = get_delayed(delayed)
    filenames, _, axis_coords = slice_directory(
        root_dir, pattern, axis_order=axis_order, slices=slices
    )
    all_channels = list(axis_coords["c"])
    if include_fixation:
        for suffix in ("initial.hdf5", "init_fixation.hdf5", "fixed.hdf5"):
            filename = root_dir / suffix
            if not filename.exists():
                continue
            image = delayed(get_hdf5_frame)(filename, "data")
            image_metadata = {}  # TODO
            msg = {
                "type": "image",
                "image": image,
                "metadata": image_metadata,
                "image_type": "eaton_fish_fixation",
                "image_name": filename.stem,
            }
            yield msg
    for key, filename in filenames:
        image = delayed(get_hdf5_frame)(filename, "data")
        coords = dict(zip(axis_order, (_numpy_to_py(k) for k in key)))
        image_metadata = {
            "channel": coords["c"],
            # TODO: do we want to send this?
            "all_channels": all_channels,
            "fov_num": coords["v"],
            "t": coords["t"] - 1,  # convert from one-based to zero-based indexing
        }
        msg = {
            "type": "image",
            "image": image,
            "metadata": image_metadata,
            "image_type": "eaton_fish",
        }
        yield msg
    if include_tables:
        # TODO: these are not sent sorted, but it shouldn't matter
        for metadata_filename in root_dir.glob("metadata_*.hdf5"):
            metadata = delayed(pd.read_hdf)(metadata_filename)
            msg = {
                "type": "table",
                "table": metadata,
                "table_type": "eaton_fish_metadata",
            }
            yield msg


# TODO: handle more elegantly
# (open file pool, etc.)
def get_hdf5_frame(filename, hdf5_path, slice_=()):
    with h5py.File(filename) as f:
        # load array into memory as a numpy array
        # TODO: do we ever want to memmap?
        frame = f[hdf5_path][slice_]
    return frame


def send_hdf5(filename, delayed=True):
    delayed = get_delayed(delayed)
    h5 = h5py.File(filename)
    # TODO: this is currently useless,
    # need to handle flexible file/dataset indexing (as supported by convert_nd2_to_array)
    # TODO: slicing
    # TODO: send whole-file metadata
    for channel, channel_group in h5.items():
        # TODO: fov -> fov_num?, z -> z_level (also in convert_nd2_to_hdf5)
        for fov, ary in channel_group.items():
            for z in range(ary.shape[0]):
                for t in range(ary.shape[1]):
                    image = delayed(get_hdf5_frame)(
                        filename, f"{channel}/{fov}", (z, t)
                    )
                    image_metadata = {
                        "dummy_metadata": 0,
                        "channel": channel,
                        "z_level": z,
                        "fov_num": fov,
                        "t": t,
                    }
                    msg = {"type": "image", "image": image, "metadata": image_metadata}
                    yield msg


def send_calibration(dark, flats, delayed=True):
    delayed = get_delayed(delayed)
    yield {
        "type": "image",
        "image_type": "calibration",
        "metadata": {"calibration_type": "dark"},
        "image": delayed(imread)(dark),
    }
    for channel, filename in flats.items():
        yield {
            "type": "image",
            "image_type": "calibration",
            "metadata": {"calibration_type": "flat", "channel": channel},
            "image": delayed(imread)(filename),
        }


def convert_nd2_to_array(
    nd2_filename,
    output_filename,
    file_axes=[],
    dataset_axes=["fov", "channel"],
    slice_axes=None,
    chunks=dict(
        z=None,
        t=5,
        channel_idx=1,
        fov=1,
        height=None,
        width=None,
    ),
    slices={},
    format="zarr",
    **kwargs,
):
    if not isinstance(output_filename, str):
        raise ValueError(
            "output_filename must be a string, and should include a trailing slash (/) if it is a directory"
        )
    if format not in ["zarr", "hdf5"]:
        raise ValueError("format must be zarr or hdf5")
    file_and_dataset_axes = set(file_axes) | set(dataset_axes)
    unknown_axes = file_and_dataset_axes - set(
        ["t", "fov", "channel", "channel_num", "z"]
    )
    if unknown_axes:
        raise ValueError(f"unknown axes: {', '.join(unknown_axes)}")
    if slice_axes is None:
        slice_axes = []
        if not set(["fov", "fov_idx"]) & file_and_dataset_axes:
            slice_axes.append("fov_idx")
        if not set(["t", "t_idx"]) & file_and_dataset_axes:
            slice_axes.append("t_idx")
        if not set(["channel", "channel_num"]) & file_and_dataset_axes:
            slice_axes.append("channel_idx")
        if "z" not in file_and_dataset_axes:
            slice_axes.append("z_idx")
    # the keys given to dataset_shape_func and dataset_chunks_func
    # have keys named channel, t (not channel_idx, t_idx)
    dataset_creation_axes = [axis.split("_")[0] for axis in slice_axes]
    if file_axes:
        output_filename += "_".join([f"{a}={{{a}}}" for a in file_axes])
    if format == "zarr":
        if not (output_filename.lower().endswith(".zarr")):
            output_filename += ".zarr"
    elif format == "hdf5":
        if not (
            output_filename.lower().endswith(".hdf5")
            or output_filename.lower().endswith(".h5")
        ):
            output_filename += ".hdf5"

    def filename_func(key):
        return output_filename.format(
            **{axis: v for axis, v in key.items() if axis in file_axes}
        )

    def dataset_func(key):
        return "/".join(f"{axis}={key[axis]}" for axis in dataset_axes)

    def slice_func(key):
        return (*(key[axis] for axis in slice_axes), slice(None), slice(None))

    def dataset_shape_func(key):
        return (
            *(len(key[axis]) for axis in dataset_creation_axes),
            key["height"],
            key["width"],
        )

    def dataset_chunks_func(key):
        return tuple(
            (
                dataset_shape_func(key)[axis_idx]
                if chunks[axis] is None
                else min(chunks[axis], dataset_shape_func(key)[axis_idx])
            )
            for axis_idx, axis in enumerate([*dataset_creation_axes, "height", "width"])
        )

    _convert_nd2_to_array(
        nd2_filename,
        filename_func,
        dataset_func,
        slice_func,
        dataset_shape_func,
        dataset_chunks_func,
        slices=slices,
        format=format,
        **kwargs,
    )


def _convert_nd2_to_array(
    nd2_filename,
    filename_func,
    dataset_func,
    slice_func,
    dataset_shape_func,
    dataset_chunks_func,
    slices={},
    format="zarr",
    zarr_store_func=None,
):
    if format not in ["zarr", "hdf5"]:
        raise ValueError("format must be zarr or hdf5")
    if "channel" in slices and "channel_num" in slices:
        raise ValueError("cannot specify both channel and channel_num slices")
    if isinstance(nd2_filename, nd2reader.ND2Reader):
        nd2 = nd2_filename
    else:
        nd2 = get_nd2_reader(nd2_filename)
    nd2_channels = nd2.metadata["channels"]
    frame = nd2.get_frame_2D()
    dtype = frame.dtype
    shape = frame.shape
    del frame
    if "channel" in slices:
        channel_slices = slices["channel"]
        if isinstance(channel_slices, slice):
            slices["channel_num"] = slice(
                nd2_channels.index(channel_slices.start),
                nd2_channels.index(channel_slices.stop),
                channel_slices.step,
            )
        else:
            slices["channel_num"] = [nd2_channels.index(c) for c in channel_slices]
    channel_nums = _select_indices(
        np.arange(nd2.sizes.get("c", 1)), slices.get("channel_num", slice(None))
    )
    fovs = _select_indices(
        np.arange(nd2.sizes.get("v", 1)), slices.get("fov", slice(None))
    )
    zs = _select_indices(np.arange(nd2.sizes.get("z", 1)), slices.get("z", slice(None)))
    ts = _select_indices(np.arange(nd2.sizes.get("t", 1)), slices.get("t", slice(None)))
    output_files = {}
    try:
        for channel_idx, channel_num in enumerate(
            tqdm(channel_nums, desc="c", leave=None)
        ):
            channel = nd2_channels[channel_idx]
            for fov_idx, fov in enumerate(tqdm(fovs, desc="v", leave=None)):
                for z_idx, z in enumerate(tqdm(zs, desc="z", leave=None)):
                    for t_idx, t in enumerate(tqdm(ts, desc="t", leave=None)):
                        key = dict(
                            fov=fov,
                            fov_idx=fov_idx,
                            channel=channel,
                            channel_num=channel_num,
                            channel_idx=channel_idx,
                            z=z,
                            z_idx=z_idx,
                            t=t,
                            t_idx=t_idx,
                        )
                        frame = nd2.get_frame_2D(c=channel_num, v=fov, z=z, t=t)
                        output_filename = filename_func(key)
                        Path(output_filename).parent.mkdir(parents=True, exist_ok=True)
                        output_file = output_files.get(output_filename)
                        if output_file is None:
                            if format == "zarr":
                                if zarr_store_func:
                                    store = zarr_store_func(output_filename)
                                    zarr_file = zarr.group(store=store)
                                else:
                                    zarr_file = zarr.hierarchy.open_group(
                                        output_filename, "a"
                                    )
                                output_file = output_files[output_filename] = zarr_file
                            elif format == "hdf5":
                                output_file = output_files[output_filename] = h5py.File(
                                    output_filename, "a"
                                )
                        dataset_path = dataset_func(key)
                        if dataset_path not in output_file:
                            dataset_key = dict(
                                # this key is called channel, not channel_nums
                                # for naming consistency in the key that is
                                # passed to dataset_shape_func and
                                # dataset_chunks_func
                                channel=channel_nums,
                                fov=fovs,
                                z=zs,
                                t=ts,
                                height=shape[0],
                                width=shape[1],
                            )
                            dataset_shape = dataset_shape_func(dataset_key)
                            dataset_chunks = dataset_chunks_func(dataset_key)
                            output_file.create_dataset(
                                dataset_path,
                                shape=dataset_shape,
                                chunks=dataset_chunks,
                                dtype=dtype,
                            )
                        slice_ = slice_func(key)
                        output_file[dataset_path][slice_] = frame

    finally:
        for output_file in output_files.values():
            if format == "zarr":
                output_file.store.close()
            elif format == "hdf5":
                output_file.close()


class ZarrSlicer:
    def __init__(self, *args, fill_value=np.nan, **kwargs):
        self.fill_value = fill_value
        sources, unmatched, axis_coords = slice_directory(*args, **kwargs)
        self.sources = dict(sources)
        self.source_key_length = len(first(self.sources.keys()))
        self.array_dim = len(self._open_array(first(self.sources.values())).shape)
        self.unmatched = unmatched
        self.axis_coords = list(map(list, axis_coords.values()))

    def _open_array(self, path):
        return zarr.open(path, mode="r")

    @staticmethod
    def _get_slice(values, slice_):
        if isinstance(slice_, slice):
            if slice_.start is None:
                start = None
            else:
                start = values.index(slice_.start)
            if slice_.stop is None:
                stop = None
            else:
                stop = values.index(slice_.stop) + 1
            if slice_.step is None:
                step = 1
            else:
                step = slice_.step
            return values[start:stop:step]
        elif isinstance(slice_, list):
            missing = set(slice_) - set(values)
            if missing:
                raise ValueError(f"invalid indices: {list(missing)}")
            return slice_
        else:
            if slice_ not in values:
                raise ValueError(f"invalid index: {slice_}")
            return [slice_]

    def __getitem__(self, slice_):
        if not isinstance(slice_, tuple):
            slice_ = (slice_,)
        slice_ = slice_ + (self.source_key_length + self.array_dim - len(slice_)) * (
            slice(None),
        )
        selected_axis_coords = [
            self._get_slice(self.axis_coords[idx], slice_[idx])
            for idx in range(self.source_key_length)
        ]
        array_slice = slice_[self.source_key_length :]
        selected_sources = {
            source_key: self._open_array(path)[array_slice]
            for source_key, path in self.sources.items()
            if all(k in sel for k, sel in zip(source_key, selected_axis_coords))
        }
        max_shape = np.max([ary.shape for ary in selected_sources.values()], axis=0)
        shape = [len(k) for k in selected_axis_coords] + list(max_shape)
        output = np.full(shape, self.fill_value)
        for source_key, ary in selected_sources.items():
            idxs = tuple(
                [
                    values.index(value)
                    for value, values in zip(source_key, selected_axis_coords)
                ]
            )
            output[idxs] = pad(ary, max_shape, fill_value=self.fill_value)
        return output
