from functools import cached_property

from Bio.Seq import Seq
from Bio.SeqUtils import GC

import paulssonlab.cloning.thermodynamics as thermodynamics
from paulssonlab.cloning.sequence import (
    enumerate_primer_binding_sites,
    get_seq,
    normalize_seq,
    reverse_complement,
    smoosh_and_trim_flanks,
)
from paulssonlab.cloning.viennarna import dna_heterodimer, dna_secondary_structure
from paulssonlab.util import any_not_none, format_number

PRIMER3_DEFAULT_TM = (55, 65, 72)


class Primer:
    def __init__(
        self,
        overhang=None,
        binding=None,
        template=None,
        binding_length=None,
        overhang_length=None,
        primer3=None,
        mfe_monomer=None,
        mfe_homodimer=None,
        tm=None,
        tm_func=thermodynamics.tm,
        tm_kwargs=None,
    ):
        if mfe_monomer is not None:
            self.mfe_monomer = mfe_monomer
        if mfe_homodimer is not None:
            self.mfe_homodimer = mfe_homodimer
        if tm is not None:
            self.tm = tm
        self.tm_func = tm_func
        self.tm_kwargs = tm_kwargs
        self.primer3 = primer3
        if self.primer3 is not None:
            if any_not_none(
                overhang, binding, template, binding_length, overhang_length
            ):
                raise ValueError(
                    "if primer3 is specified, cannot also specify other primer sequences/lengths"
                )
            self.binding = Seq(primer3["SEQUENCE"])
            self.overhang = Seq(primer3["OVERHANG"])
            self.gc = primer3["GC_PERCENT"]
        else:
            if overhang is not None:
                if binding is not None:
                    if any_not_none(template, binding_length, overhang_length):
                        raise ValueError(
                            "if binding and overhang are specified, cannot also specify template, binding_length, or overhang_length"
                        )
                    self.overhang = overhang
                    self.binding = binding
                else:
                    if binding_length is not None:
                        if overhang_length is not None:
                            raise ValueError(
                                "cannot specify both binding_length and overhang_length"
                            )
                        overhang_length = len(seq) - binding_length
                        self.binding = overhang[overhang_length:]
                        self.overhang = overhang[:overhang_length]
                    else:
                        if overhang_length is not None:
                            self.binding = overhang[overhang_length:]
                            self.overhang = overhang[:overhang_length]
                        else:
                            if template is not None:
                                self.overhang, self.binding, _ = get_primer_overhang(
                                    template, overhang
                                )
                            else:
                                # cannot have a primer with overhang but no binding
                                # so if only one sequence is specified as a positional arg,
                                # assume it is the binding region
                                self.binding = overhang
                                self.overhang = overhang[:0]  # zero-length of same type
            else:
                if binding is not None:
                    self.binding = binding
                    self.overhang = binding[:0]  # zero-length of same type
                else:
                    raise ValueError("need to specify primer sequence")

    @property
    def seq(self):
        return self.overhang + self.binding

    def __len__(self):
        return len(self.overhang) + len(self.binding)

    @cached_property
    def gc(self):
        return GC(self.seq)

    @cached_property
    def tm(self):
        return self.tm_func(self.binding, **(self.tm_kwargs or {}))

    @cached_property
    def mfe_monomer(self):
        self._calculate_thermodynamics()
        return self.mfe_monomer

    @cached_property
    def mfe_homodimer(self):
        self._calculate_thermodynamics()
        return self.mfe_homodimer

    def _calculate_thermodynamics(self):
        self.mfe_monomer, self.mfe_homodimer = dna_secondary_structure(self.seq)

    def __str__(self):
        return str(get_seq(self.seq))

    def __repr__(self):
        tm = self.__dict__.get("tm")
        mfe_monomer = self.__dict__.get("mfe_monomer")
        mfe_homodimer = self.__dict__.get("mfe_homodimer")
        primer3 = "{...}" if self.primer3 else "None"
        return f"Primer(overhang={self.overhang}, binding={self.binding}, tm={format_number('{:.1f}', tm)}, mfe_monomer={format_number('{:.1f}', mfe_monomer)}, mfe_homodimer={format_number('{:.1f}', mfe_homodimer)}, primer3={primer3})"


class PrimerPair:
    def __init__(
        self,
        primer1=None,
        primer2=None,
        template=None,
        primer3=None,  # this refers to the Primer3 program, not the third primer after primer1/2!
        mfe_heterodimer=None,
        ta=None,
        ta_func=thermodynamics.ta_from_tms,
        ta_kwargs=None,
    ):
        if mfe_heterodimer is not None:
            self.mfe_heterodimer = mfe_heterodimer
        if ta is not None:
            self.ta = ta
        self.ta_func = ta_func
        self.ta_kwargs = ta_kwargs
        self.primer3 = primer3
        if self.primer3 is not None:
            if any_not_none(primer1, primer2, template):
                raise ValueError(
                    "if primer3 is specified, cannot also specify primers and/or template"
                )
            self.primer1 = Primer(primer3=primer3["LEFT"])
            self.primer2 = Primer(primer3=primer3["RIGHT"])
        else:
            if not isinstance(primer1, Primer):
                primer1 = Primer(primer1, template=template)
            if not isinstance(primer2, Primer):
                primer2 = Primer(primer2, template=template)
            self.primer1 = primer1
            self.primer2 = primer2

    @property
    def min_length(self):
        return min(len(self.primer1), len(self.primer2))

    @property
    def max_length(self):
        return max(len(self.primer1), len(self.primer2))

    @property
    def min_binding_length(self):
        return min(len(self.primer1.binding), len(self.primer2.binding))

    @property
    def max_binding_length(self):
        return max(len(self.primer1.binding), len(self.primer2.binding))

    @property
    def min_overhang_length(self):
        return min(len(self.primer1.overhang), len(self.primer2.overhang))

    @property
    def max_overhang_length(self):
        return max(len(self.primer1.overhang), len(self.primer2.overhang))

    @property
    def min_gc(self):
        return min(self.primer1.gc, self.primer2.gc)

    @property
    def max_gc(self):
        return max(self.primer1.gc, self.primer2.gc)

    @property
    def min_tm(self):
        return min(self.primer1.tm, self.primer2.tm)

    @property
    def max_tm(self):
        return max(self.primer1.tm, self.primer2.tm)

    @cached_property
    def ta(self):
        return self.ta_func(self.primer1.tm, self.primer2.tm, **(self.ta_kwargs or {}))

    @cached_property
    def mfe_heterodimer(self):
        return dna_heterodimer(self.primer1.seq, self.primer2.seq)

    def __repr__(self):
        ta = self.__dict__.get("ta")
        mfe_heterodimer = self.__dict__.get("mfe_heterodimer")
        primer3 = "{...}" if self.primer3 else "None"
        return f"PrimerPair(primer1={self.primer1!r},\n           primer2={self.primer2!r},\n           ta={format_number('{:.1f}', ta)}, mfe_heterodimer={format_number('{:.1f}', mfe_heterodimer)}, primer3={primer3})"


# TODO: Q5/Phusion salt settings
def run_primer3(design, tm=PRIMER3_DEFAULT_TM, return_explain=False, return_many=False):
    if len(tm) != 3:
        raise ValueError("expecting (min, optimal, max) tm")
        design.params["PRIMER_OPT_TM"] = tm[0]
        design.params["PRIMER_MIN_TM"] = tm[1]
        design.params["PRIMER_MAX_TM"] = tm[2]
    if return_many is False:
        num_return = 1
    else:
        num_return = return_many
    design.settings.primer_num_return(num_return)
    # print(design.params)
    results, explain = design.run()
    if not return_many:
        if len(results) == 0:
            raise ValueError("did not design primers")
        elif len(results) > 1:
            raise NotImplementError
        primers = PrimerPair(primer3=results[0])
    else:
        primers = [PrimerPair(primer3=r) for r in results.values()]
    if return_explain:
        return primers, explain
    else:
        return primers


def primer3_product_from_template(
    product,
    template,
    min_length=100,
    tm=PRIMER3_DEFAULT_TM,
    return_explain=False,
    return_many=False,
):
    # find location of longest product/template match (ignoring ends)
    # fix 5' end of primers (HOW?)
    # set location
    # find product overhangs
    # use_overhangs

    pass


# TODO: limit length of primers/set acceptable region
def primer3_amplicon_with_flanks(
    amplicon, flanks, tm=PRIMER3_DEFAULT_TM, return_explain=False, return_many=False
):
    import primer3plus  # TODO: unmaintained

    amplicon_seq = str(get_seq(amplicon)).lower()
    trimmed_flanks = smoosh_and_trim_flanks(amplicon_seq, overhangs)
    design = primer3plus.Design()
    design.settings.template(amplicon_seq)
    design.settings.as_cloning_task()
    design.settings.use_overhangs()
    design.settings.left_overhang(trimmed_flanks[0])
    design.settings.right_overhang(trimmed_flanks[1])  # TODO: reverse complement?
    design.settings.product_size([27, 10000], opt=0)
    return run_primer3(
        design, tm=tm, return_explain=return_explain, return_many=return_many
    )


def iter_bounds(
    length,
    min_length=None,
    max_length=None,
    anchor="stop",
    outer="stop",
    reverse_inner=False,
    reverse_outer=False,
):
    """
    Parameters
    ----------
    length : int
        Length of sequence.
    min_length : int, optional
        Minimum subsequence length, or None.
    max_length : None, optional
        Maximum subsequence length, or None.
    anchor : str, optional
        Must be one of "start", "stop", or None/False. Specifies if either "start" or "stop" remains fixed.
    outer : str, optional
        Must be one of "start", "stop", or "length". Specifies which parameter is changed by the outermost iteration.
    reverse_inner : bool, optional
        Reverse the inner iteration.
    reverse_outer : bool, optional
        Reverse the outer iteration.

    Yields
    -----------------
    (start, stop) : (int, int)
        Yields all possible indexes for slicing into a sequence of length `length` where the subsequence has length between `min_length` and `max_length`.

    """
    reverse_if = lambda cond, iterable: reversed(iterable) if cond else iterable
    if length < 1:
        raise ValueError("length must be >= 1")
    if min_length is None:
        min_length = 1
    if max_length is None:
        max_length = length
    else:
        max_length = min(max_length, length)
    if anchor not in ("start", "stop", None, False):
        raise ValueError("anchor must be 'start', 'stop', or None/False")
    if outer not in ("start", "stop", "length"):
        raise ValueError("outer must be 'start', 'stop', or 'length'")
    start_idxs = None
    if anchor == "start":
        outer = "start"
        start_idxs = [0]
    elif anchor == "stop":
        outer = "stop"
        anchor = "start"
    elif not anchor:
        pass
    else:
        raise NotImplementError
    if outer == "start":
        if start_idxs is None:
            start_idxs = range(0, length - min_length + 1)
        stop_idxs = lambda start: reverse_if(
            reverse_inner,
            range(start + min_length, min(start + max_length, length) + 1),
        )
        for start in reverse_if(reverse_outer, start_idxs):
            for stop in stop_idxs(start):
                yield start, stop
    elif outer == "stop":
        for start, stop in iter_bounds(
            length,
            min_length=min_length,
            max_length=max_length,
            anchor=anchor,
            outer="start",
            reverse_inner=reverse_inner,
            reverse_outer=not reverse_outer,
        ):
            yield length - stop, length - start
    elif outer == "length":
        lengths = range(min_length, max_length + 1)
        start_idxs = lambda subseq_length: reverse_if(
            reverse_inner, range(0, length - subseq_length + 1)
        )
        for subseq_length in reverse_if(reverse_outer, lengths):
            for start in start_idxs(subseq_length):
                yield start, start + subseq_length
    else:
        raise NotImplementError


# TODO: you should probably just use primer3 instead,
# this works but have not implemented a method for picking primer pairs
def iter_primers(
    seq,
    overhang=None,
    min_length=10,
    min_tm=60,
    max_tm=72,
    min_mfe=-8,
    tm_func=thermodynamics.tm,
    monotonic_mfe=False,
    tm_kwargs=None,
    gc_clamp=True,
    anchor=None,
):
    if anchor not in ("3prime", "5prime", None, False):
        raise ValueError("anchor must be one of '3prime', '5prime', or None/False")
    if anchor == "3prime":
        bounds = iter_bounds(len(seq), min_length=min_length, anchor="stop")
    elif anchor == "5prime":
        bounds = iter_bounds(len(seq), min_length=min_length, anchor="start")
    else:
        bounds = iter_bounds(len(seq), min_length=min_length)
    yield from _iter_primers(
        seq,
        overhang=overhang,
        min_tm=min_tm,
        max_tm=max_tm,
        min_mfe=min_mfe,
        tm_func=tm_func,
        monotonic_mfe=monotonic_mfe,
        tm_kwargs=tm_kwargs,
        gc_clamp=gc_clamp,
        bounds=bounds,
    )


def _iter_primers(
    seq,
    overhang=None,
    min_tm=60,
    max_tm=72,
    min_mfe=-8,
    tm_func=thermodynamics.tm,
    monotonic_mfe=False,
    tm_kwargs=None,
    gc_clamp=True,
    bounds=None,
):
    if bounds is None:
        bounds = iter_bounds(len(seq))
    elif callable(bounds):
        bounds = bounds()
    for start, stop in bounds:
        binding_seq = seq[start:stop]
        if gc_clamp:
            if normalize_seq(binding_seq[-1]) not in ("g", "c"):
                continue
        if overhang is not None:
            primer_seq = overhang + binding_seq
        else:
            primer_seq = binding_seq
        tm = tm_func(binding_seq, **(tm_kwargs or {}))
        if tm < min_tm:
            continue
        elif tm > max_tm:
            break
        mfe_monomer, mfe_homodimer = dna_secondary_structure(primer_seq)
        if min_mfe is not None and min(mfe_monomer, mfe_homodimer) < min_mfe:
            if monotonic_mfe:
                # if we assume mfe monotonically decreases with length, then we can stop extending
                # this is probably not exactly true but pretty close, and likely speeds things up
                break
            else:
                continue
        primer = Primer(
            overhang=overhang,
            binding=binding_seq,
            tm=tm,
            mfe_monomer=mfe_monomer,
            mfe_homodimer=mfe_homodimer,
        )
        yield primer


def get_primer_overhang(template, primer):
    sites = enumerate_primer_binding_sites(template, primer)
    if len(sites) != 1:
        raise ValueError(
            f"expecting a unique primer binding site, instead got {len(sites)}"
        )
    sense = sites[0].strand
    score = sites[0].score
    overlap = primer[sites[0].seq2_start : sites[0].seq2_stop]
    binding = overlap[len(overlap) - score :]
    overhang = primer[: len(primer) - len(binding)]
    return overhang, binding, sense


def replace_primer_overhang(template, primer, new_overhang=""):
    overhang, binding, sense = get_primer_overhang(template, primer)
    if sense == -1:
        new_overhang = reverse_complement(new_overhang)
    new_primer = new_overhang + binding
    return new_primer


def format_primer_orientation(template, primer, format="f"):
    uppercase_initial = format[0].isupper()
    uppercase_all = format[-1].isupper()
    format = format.lower()
    sites = enumerate_primer_binding_sites(template, primer)
    if len(sites) != 1:
        raise ValueError(
            f"expecting a unique primer binding site, instead got {len(sites)}"
        )
    sense = sites[0][0]
    if format in ("r", "f"):
        if sense == -1:
            s = "r"
        else:
            s = "f"
    elif format in ("rev", "fwd"):
        if sense == -1:
            s = "rev"
        else:
            s = "fwd"
    elif format in ("reverse", "forward"):
        if sense == -1:
            s = "reverse"
        else:
            s = "forward"
    else:
        raise ValueError(f"unknown format string {format}")
    if uppercase_all:
        return s.upper()
    else:
        if uppercase_initial:
            return s.capitalize()
        else:
            return s
