from __future__ import division
# Copyright (c) 2010-2015, GEM Foundation.
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake.  If not, see <http://www.gnu.org/licenses/>.

import mock
import time
import logging
import operator
import itertools
import collections
import random
from xml.etree import ElementTree as etree

import numpy

from openquake.baselib.general import AccumDict, groupby
from openquake.commonlib.node import read_nodes
from openquake.commonlib import valid, logictree, sourceconverter, parallel
from openquake.commonlib.nrml import nodefactory, PARSE_NS_MAP
from functools import reduce


class DuplicatedID(Exception):
    """Raised when two sources with the same ID are found in a source model"""


class LtRealization(object):
    """
    Composite realization build on top of a source model realization and
    a GSIM realization.
    """
    def __init__(self, ordinal, sm_lt_path, gsim_rlz, weight):
        self.ordinal = ordinal
        self.sm_lt_path = sm_lt_path
        self.gsim_rlz = gsim_rlz
        self.weight = weight

    def __repr__(self):
        return '<%d,%s,w=%s>' % (self.ordinal, self.uid, self.weight)

    @property
    def gsim_lt_path(self):
        return self.gsim_rlz.lt_path

    @property
    def uid(self):
        """An unique identifier for effective realizations"""
        return '_'.join(self.sm_lt_path) + ',' + self.gsim_rlz.uid

    def __eq__(self, other):
        return repr(self) == repr(other)

    def __ne__(self, other):
        return repr(self) != repr(other)

    def __hash__(self):
        return hash(repr(self))


def get_skeleton(sm):
    """
    Return a copy of the source model `sm` which is empty, i.e. without
    sources.
    """
    trt_models = [TrtModel(tm.trt, [], tm.num_ruptures, tm.min_mag,
                           tm.max_mag, tm.gsims, tm.id)
                  for tm in sm.trt_models]
    num_sources = sum(len(tm) for tm in sm.trt_models)
    return SourceModel(sm.name, sm.weight, sm.path, trt_models, sm.gsim_lt,
                       sm.ordinal, sm.samples, num_sources)

SourceModel = collections.namedtuple(
    'SourceModel', 'name weight path trt_models gsim_lt ordinal samples '
    'num_sources')


def get_weight(src, point_source_weight=1/40., num_ruptures=None):
    """
    :param src: a hazardlib source object
    :param point_source_weight: default 1/40
    :param num_ruptures: if None it is recomputed
    :returns: the weight of the given source
    """
    num_ruptures = num_ruptures or src.count_ruptures()
    weight = (num_ruptures * point_source_weight
              if src.__class__.__name__ == 'PointSource'
              else num_ruptures)
    return weight


class TrtModel(collections.Sequence):
    """
    A container for the following parameters:

    :param str trt:
        the tectonic region type all the sources belong to
    :param list sources:
        a list of hazardlib source objects
    :param int num_ruptures:
        the total number of ruptures generated by the given sources
    :param min_mag:
        the minimum magnitude among the given sources
    :param max_mag:
        the maximum magnitude among the given sources
    :param gsims:
        the GSIMs associated to tectonic region type
    :param id:
        an optional numeric ID (default None) useful to associate
        the model to a database object
    """
    POINT_SOURCE_WEIGHT = 1 / 40.

    @classmethod
    def collect(cls, sources):
        """
        :param sources: dictionaries with a key 'tectonicRegion'
        :returns: an ordered list of TrtModel instances
        """
        source_stats_dict = {}
        for src in sources:
            trt = src['tectonicRegion']
            if trt not in source_stats_dict:
                source_stats_dict[trt] = TrtModel(trt)
            tm = source_stats_dict[trt]
            if not tm.sources:

                # we increate the rupture counter by 1,
                # to avoid filtering away the TRTModel
                tm.num_ruptures = 1

                # we append just one source per TRTModel, so that
                # the memory occupation is insignificand and at
                # the same time we avoid the RuntimeError
                # "All sources were filtered away"
                tm.sources.append(src)

        # return TrtModels, ordered by TRT string
        return sorted(source_stats_dict.values())

    def __init__(self, trt, sources=None, num_ruptures=0,
                 min_mag=None, max_mag=None, gsims=None, id=0):
        self.trt = trt
        self.sources = sources or []
        self.num_ruptures = num_ruptures
        self.min_mag = min_mag
        self.max_mag = max_mag
        self.gsims = gsims or []
        self.id = id
        for src in self.sources:
            self.update(src)
        self.source_model = None  # to be set later, in CompositionInfo

    def update(self, src):
        """
        Update the attributes sources, min_mag, max_mag
        according to the given source.

        :param src:
            an instance of :class:
            `openquake.hazardlib.source.base.BaseSeismicSource`
        """
        assert src.tectonic_region_type == self.trt, (
            src.tectonic_region_type, self.trt)
        self.sources.append(src)
        min_mag, max_mag = src.get_min_max_mag()
        prev_min_mag = self.min_mag
        if prev_min_mag is None or min_mag < prev_min_mag:
            self.min_mag = min_mag
        prev_max_mag = self.max_mag
        if prev_max_mag is None or max_mag > prev_max_mag:
            self.max_mag = max_mag

    def __repr__(self):
        return '<%s #%d %s, %d source(s), %d rupture(s)>' % (
            self.__class__.__name__, self.id, self.trt,
            len(self.sources), self.num_ruptures)

    def __lt__(self, other):
        """
        Make sure there is a precise ordering of TrtModel objects.
        Objects with less sources are put first; in case the number
        of sources is the same, use lexicographic ordering on the trts
        """
        num_sources = len(self.sources)
        other_sources = len(other.sources)
        if num_sources == other_sources:
            return self.trt < other.trt
        return num_sources < other_sources

    def __getitem__(self, i):
        return self.sources[i]

    def __iter__(self):
        return iter(self.sources)

    def __len__(self):
        return len(self.sources)


def parse_source_model(fname, converter, apply_uncertainties=lambda src: None):
    """
    Parse a NRML source model and return an ordered list of TrtModel
    instances.

    :param str fname:
        the full pathname of the source model file
    :param converter:
        :class:`openquake.commonlib.source.SourceConverter` instance
    :param apply_uncertainties:
        a function modifying the sources (or do nothing)
    """
    converter.fname = fname
    source_stats_dict = {}
    source_ids = set()
    src_nodes = read_nodes(fname, lambda elem: 'Source' in elem.tag,
                           nodefactory['sourceModel'])
    for no, src_node in enumerate(src_nodes, 1):
        src = converter.convert_node(src_node)
        if src.source_id in source_ids:
            raise DuplicatedID(
                'The source ID %s is duplicated!' % src.source_id)
        apply_uncertainties(src)
        trt = src.tectonic_region_type
        if trt not in source_stats_dict:
            source_stats_dict[trt] = TrtModel(trt)
        source_stats_dict[trt].update(src)
        source_ids.add(src.source_id)
        if no % 10000 == 0:  # log every 10,000 sources parsed
            logging.info('Parsed %d sources from %s', no, fname)

    # return ordered TrtModels
    return sorted(source_stats_dict.values())


def agg_prob(acc, prob):
    """Aggregation function for probabilities"""
    return 1. - (1. - acc) * (1. - prob)


class RlzsAssoc(collections.Mapping):
    """
    Realization association class. It should not be instantiated directly,
    but only via the method :meth:
    `openquake.commonlib.source.CompositeSourceModel.get_rlzs_assoc`.

    :attr realizations: list of LtRealization objects
    :attr gsim_by_trt: list of dictionaries {trt: gsim}
    :attr rlzs_assoc: dictionary {trt_model_id, gsim: rlzs}
    :attr rlzs_by_smodel: list of lists of realizations

    For instance, for the non-trivial logic tree in
    :mod:`openquake.qa_tests_data.classical.case_15`, which has 4 tectonic
    region types and 4 + 2 + 2 realizations, there are the following
    associations:

    (0, 'BooreAtkinson2008') ['#0-SM1-BA2008_C2003', '#1-SM1-BA2008_T2002']
    (0, 'CampbellBozorgnia2008') ['#2-SM1-CB2008_C2003', '#3-SM1-CB2008_T2002']
    (1, 'Campbell2003') ['#0-SM1-BA2008_C2003', '#2-SM1-CB2008_C2003']
    (1, 'ToroEtAl2002') ['#1-SM1-BA2008_T2002', '#3-SM1-CB2008_T2002']
    (2, 'BooreAtkinson2008') ['#4-SM2_a3pt2b0pt8-BA2008']
    (2, 'CampbellBozorgnia2008') ['#5-SM2_a3pt2b0pt8-CB2008']
    (3, 'BooreAtkinson2008') ['#6-SM2_a3b1-BA2008']
    (3, 'CampbellBozorgnia2008') ['#7-SM2_a3b1-CB2008']
    """
    def __init__(self, csm_info):
        self.csm_info = csm_info
        self.rlzs_assoc = collections.defaultdict(list)
        self.gsim_by_trt = []  # rlz.ordinal -> {trt: gsim}
        self.rlzs_by_smodel = [[] for _ in range(len(csm_info.source_models))]
        self.gsims_by_trt_id = {}
        self.col_ids_by_rlz = collections.defaultdict(set)

    @property
    def num_samples(self):
        """
        Underlying number_of_logic_tree_samples
        """
        return self.csm_info.source_model_lt.num_samples

    @property
    def realizations(self):
        """Flat list with all the realizations"""
        return sum(self.rlzs_by_smodel, [])

    def get_gsims_by_col(self):
        """Return a list of lists of GSIMs of length num_collections"""
        # TODO: add a special case for sampling?
        return [self.gsims_by_trt_id.get(col['trt_id'], [])
                for col in self.csm_info.cols]

    # this useful to extract the ruptures affecting a given realization
    def get_col_ids(self, rlz):
        """
        :param rlz: a realization
        :returns: a set of ses collection indices relevant for the realization
        """
        # first consider the oversampling case, when the col_ids are known
        col_ids = self.col_ids_by_rlz[rlz]
        if col_ids:
            return col_ids
        # else consider the source model to which the realization belongs
        # and extract the trt_model_ids, which are the same as the col_ids
        return set(tm.id for sm in self.csm_info.source_models
                   for tm in sm.trt_models if sm.path == rlz.sm_lt_path)

    def _add_realizations(self, idx, lt_model, realizations, trts):
        gsim_lt = lt_model.gsim_lt
        rlzs = []
        for i, gsim_rlz in enumerate(realizations):
            weight = float(lt_model.weight) * float(gsim_rlz.weight)
            rlz = LtRealization(idx, lt_model.path, gsim_rlz, weight)
            self.gsim_by_trt.append(dict(
                zip(gsim_lt.all_trts, gsim_rlz.value)))
            for trt_model in lt_model.trt_models:
                if trt_model.trt in trts:
                    # ignore the associations to discarded TRTs
                    gs = gsim_lt.get_gsim_by_trt(gsim_rlz, trt_model.trt)
                    self.rlzs_assoc[trt_model.id, gs].append(rlz)
                if lt_model.samples > 1:  # oversampling
                    col_id = self.csm_info.col_ids_by_trt_id[trt_model.id][i]
                    self.col_ids_by_rlz[rlz].add(col_id)
            idx += 1
            rlzs.append(rlz)
        self.rlzs_by_smodel[lt_model.ordinal] = rlzs
        return idx

    def combine_curves(self, results, agg, acc):
        """
        :param results: dictionary (trt_model_id, gsim_name) -> curves
        :param agg: aggregation function (composition of probabilities)
        :returns: a dictionary rlz -> aggregated curves
        """
        ad = AccumDict({rlz: acc for rlz in self.realizations})
        for key, value in results.items():
            for rlz in self.rlzs_assoc[key]:
                ad[rlz] = agg(ad[rlz], value)
        return ad

    def combine_gmfs(self, gmfs):
        """
        :param gmfs: datastore /gmfs object
        :returns: a list of dictionaries rupid -> gmf array
        """
        gsims_by_col = self.get_gsims_by_col()
        dicts = [{} for rlz in self.realizations]
        for col_id, gsims in enumerate(gsims_by_col):
            try:
                dataset = gmfs['col%02d' % col_id]
            except KeyError:  # empty dataset
                continue
            trt_id = self.csm_info.get_trt_id(col_id)
            gmfs_by_rupid = groupby(
                dataset.value, lambda row: row['idx'], list)
            for gsim in gsims:
                gs = str(gsim)
                for rlz in self.rlzs_assoc[trt_id, gs]:
                    col_ids = self.col_ids_by_rlz[rlz]
                    if not col_ids or col_id in col_ids:
                        for rupid, rows in gmfs_by_rupid.items():
                            dicts[rlz.ordinal][rupid] = numpy.array(
                                [r[gs] for r in rows], rows[0][gs].dtype)
        return dicts

    def combine(self, results, agg=agg_prob):
        """
        :param results: a dictionary (trt_model_id, gsim_name) -> floats
        :param agg: an aggregation function
        :returns: a dictionary rlz -> aggregated floats

        Example: a case with tectonic region type T1 with GSIMS A, B, C
        and tectonic region type T2 with GSIMS D, E.

        >>> assoc = RlzsAssoc(CompositionInfo([], []))
        >>> assoc.rlzs_assoc = {
        ... ('T1', 'A'): ['r0', 'r1'],
        ... ('T1', 'B'): ['r2', 'r3'],
        ... ('T1', 'C'): ['r4', 'r5'],
        ... ('T2', 'D'): ['r0', 'r2', 'r4'],
        ... ('T2', 'E'): ['r1', 'r3', 'r5']}
        ...
        >>> results = {
        ... ('T1', 'A'): 0.01,
        ... ('T1', 'B'): 0.02,
        ... ('T1', 'C'): 0.03,
        ... ('T2', 'D'): 0.04,
        ... ('T2', 'E'): 0.05,}
        ...
        >>> combinations = assoc.combine(results, operator.add)
        >>> for key, value in sorted(combinations.items()): print key, value
        r0 0.05
        r1 0.06
        r2 0.06
        r3 0.07
        r4 0.07
        r5 0.08

        You can check that all the possible sums are performed:

        r0: 0.01 + 0.04 (T1A + T2D)
        r1: 0.01 + 0.05 (T1A + T2E)
        r2: 0.02 + 0.04 (T1B + T2D)
        r3: 0.02 + 0.05 (T1B + T2E)
        r4: 0.03 + 0.04 (T1C + T2D)
        r5: 0.03 + 0.05 (T1C + T2E)

        In reality, the `combine_curves` method is used with hazard_curves and
        the aggregation function is the `agg_curves` function, a composition of
        probability, which however is close to the sum for small probabilities.
        """
        ad = AccumDict()
        for key, value in results.items():
            for rlz in self.rlzs_assoc[key]:
                ad[rlz] = agg(ad.get(rlz, 0), value)
        return ad

    def __iter__(self):
        return iter(self.rlzs_assoc)

    def __getitem__(self, key):
        return self.rlzs_assoc[key]

    def __len__(self):
        return len(self.rlzs_assoc)

    def __repr__(self):
        pairs = []
        for key in sorted(self.rlzs_assoc):
            rlzs = list(map(str, self.rlzs_assoc[key]))
            if len(rlzs) > 10:  # short representation
                rlzs = ['%d realizations' % len(rlzs)]
            pairs.append(('%s,%s' % key, rlzs))
        return '<%s(%d)\n%s>' % (self.__class__.__name__, len(self),
                                 '\n'.join('%s: %s' % pair for pair in pairs))

# collection <-> trt model associations
col_dt = numpy.dtype([('trt_id', numpy.uint32), ('sample', numpy.uint32)])


class CompositionInfo(object):
    """
    An object to collect information about the composition of
    a composite source model.

    :param source_model_lt: a SourceModelLogicTree object
    :param source_models: a list of SourceModel instances
    """
    def __init__(self, source_model_lt, source_models):
        self.source_model_lt = source_model_lt
        self.source_models = source_models
        cols = []
        col_id = 0
        self.col_ids_by_trt_id = collections.defaultdict(list)
        self.tmdict = {}  # trt_id -> trt_model
        for sm in self.source_models:
            for trt_model in sm.trt_models:
                trt_model.source_model = sm
                trt_id = trt_model.id
                self.tmdict[trt_id] = trt_model
                for idx in range(sm.samples):
                    cols.append((trt_id, idx))
                    self.col_ids_by_trt_id[trt_id].append(col_id)
                    col_id += 1
        self.cols = numpy.array(cols, col_dt)

    def __getnewargs__(self):
        # with this CompositionInfo instances will be unpickled correctly
        return self.source_model_lt, self.source_models

    @property
    def num_collections(self):
        """
        Return the number of underlying collections
        """
        return len(self.cols)

    def get_num_rlzs(self, source_model=None):
        """
        :param source_model: a SourceModel instance (or None)
        :returns: the number of realizations per source model (or all)
        """
        if source_model is None:
            return sum(self.get_num_rlzs(sm) for sm in self.source_models)
        if self.source_model_lt.num_samples:
            return source_model.samples
        return source_model.gsim_lt.get_num_paths()

    def get_max_samples(self):
        """
        Return the maximum number of samples of the source model
        """
        return max(len(col_ids) for col_ids in self.col_ids_by_trt_id.values())

    def get_num_samples(self, trt_id):
        """
        :param trt_id: tectonic region type object ID
        :returns: how many times the sources of that TRT are to be sampled
        """
        return len(self.col_ids_by_trt_id[trt_id])

    def get_trt_id(self, col_id):
        """
        :param col_id: the ordinal of a SESCollection
        :returns: the ID of the associated TrtModel
        """
        for cid, col in enumerate(self.cols):
            if cid == col_id:
                return col['trt_id']
        raise KeyError('There is no TrtModel associated to the collection %d!'
                       % col_id)

    def get_triples(self):
        """
        Yield triples (trt_id, idx, col_id) in order
        """
        for col_id, col in enumerate(self.cols):
            yield col['trt_id'], col['sample'], col_id

    def __repr__(self):
        info_by_model = collections.OrderedDict(
            (sm.path, ('_'.join(sm.path), sm.name,
                       [tm.id for tm in sm.trt_models],
                       sm.weight, self.get_num_rlzs(sm)))
            for sm in self.source_models)
        summary = ['%s, %s, trt=%s, weight=%s: %d realization(s)' % ibm
                   for ibm in info_by_model.values()]
        return '<%s\n%s>' % (
            self.__class__.__name__, '\n'.join(summary))


class CompositeSourceModel(collections.Sequence):
    """
    :param source_model_lt:
        a :class:`openquake.commonlib.logictree.SourceModelLogicTree` instance
    :param source_models:
        a list of :class:`openquake.commonlib.source.SourceModel` tuples
    """
    def __init__(self, source_model_lt, source_models):
        self.source_model_lt = source_model_lt
        self.source_models = source_models
        self.source_info = ()  # set by the SourceFilterSplitter

    @property
    def trt_models(self):
        """
        Yields the TrtModels inside each source model.
        """
        for sm in self.source_models:
            for trt_model in sm.trt_models:
                yield trt_model

    def get_sources(self):
        """
        Extract the sources contained in the internal source models.
        """
        sources = []
        ordinal = 0
        for trt_model in self.trt_models:
            for src in trt_model:
                if hasattr(src, 'trt_model_id'):
                    # .trt_model_id is missing for source nodes
                    src.trt_model_id = trt_model.id
                    src.id = ordinal
                    ordinal += 1
                sources.append(src)
        return sources

    def get_num_sources(self):
        """
        :returns: the total number of sources in the model
        """
        return len(self.get_sources())

    def count_ruptures(self, really=False):
        """
        Update the attribute .num_ruptures in each TRT model.
        This method is lazy, i.e. the number is not updated if it is already
        set and nonzero, unless `really` is True.
        """
        for trt_model in self.trt_models:
            if trt_model.num_ruptures == 0 or really:
                trt_model.num_ruptures = sum(
                    src.count_ruptures() for src in trt_model)

    def get_info(self):
        """
        Return a CompositionInfo instance for the current composite model
        """
        return CompositionInfo(
            self.source_model_lt, list(map(get_skeleton, self.source_models)))

    def get_rlzs_assoc(self, get_weight=lambda tm: tm.num_ruptures):
        """
        Return a RlzsAssoc with fields realizations, gsim_by_trt,
        rlz_idx and trt_gsims.

        :param get_weight: a function trt_model -> positive number
        """
        assoc = RlzsAssoc(self.get_info())
        random_seed = self.source_model_lt.seed
        num_samples = self.source_model_lt.num_samples
        idx = 0
        for smodel in self.source_models:
            # collect the effective tectonic region types
            trts = set(tm.trt for tm in smodel.trt_models if get_weight(tm))
            # recompute the GSIM logic tree if needed
            if trts != set(smodel.gsim_lt.tectonic_region_types):
                before = smodel.gsim_lt.get_num_paths()
                smodel.gsim_lt.reduce(trts)
                after = smodel.gsim_lt.get_num_paths()
                logging.warn('Reducing the logic tree of %s from %d to %d '
                             'realizations', smodel.name, before, after)
            if num_samples:  # sampling
                rnd = random.Random(random_seed + idx)
                rlzs = logictree.sample(smodel.gsim_lt, smodel.samples, rnd)
            else:  # full enumeration
                rlzs = logictree.get_effective_rlzs(smodel.gsim_lt)
            if rlzs:
                idx = assoc._add_realizations(idx, smodel, rlzs, trts)
                for trt_model in smodel.trt_models:
                    trt_model.gsims = smodel.gsim_lt.values[trt_model.trt]
            else:
                logging.warn('No realizations for %s, %s',
                             '_'.join(smodel.path), smodel.name)
        if assoc.realizations:
            if num_samples:
                assert len(assoc.realizations) == num_samples
                for rlz in assoc.realizations:
                    rlz.weight = 1. / num_samples
            else:
                tot_weight = sum(rlz.weight for rlz in assoc.realizations)
                if tot_weight == 0:
                    raise ValueError('All realizations have zero weight??')
                elif abs(tot_weight - 1) > 1E-12:  # allow for rounding errors
                    logging.warn('Some source models are not contributing, '
                                 'weights are being rescaled')
                for rlz in assoc.realizations:
                    rlz.weight = rlz.weight / tot_weight

        assoc.gsims_by_trt_id = groupby(
            assoc.rlzs_assoc, operator.itemgetter(0),
            lambda group: sorted(valid.gsim(gsim) for trt_id, gsim in group))

        return assoc

    def __repr__(self):
        """
        Return a string representation of the composite model
        """
        models = ['%d-%s-%s,w=%s [%d trt_model(s)]' % (
            sm.ordinal, sm.name, '_'.join(sm.path), sm.weight,
            len(sm.trt_models)) for sm in self.source_models]
        return '<%s\n%s>' % (self.__class__.__name__, '\n'.join(models))

    def __getitem__(self, i):
        """Return the i-th source model"""
        return self.source_models[i]

    def __iter__(self):
        """Return an iterator over the underlying source models"""
        return iter(self.source_models)

    def __len__(self):
        """Return the number of underlying source models"""
        return len(self.source_models)


def collect_source_model_paths(smlt):
    """
    Given a path to a source model logic tree or a file-like, collect all of
    the soft-linked path names to the source models it contains and return them
    as a uniquified list (no duplicates).

    :param smlt: source model logic tree file
    """
    src_paths = []
    try:
        tree = etree.parse(smlt)
        for branch_set in tree.findall('.//nrml:logicTreeBranchSet',
                                       namespaces=PARSE_NS_MAP):

            if branch_set.get('uncertaintyType') == 'sourceModel':
                for branch in branch_set.findall(
                        './nrml:logicTreeBranch/nrml:uncertaintyModel',
                        namespaces=PARSE_NS_MAP):
                    src_paths.append(branch.text)
    except Exception as exc:
        raise Exception('%s: %s in %s' % (exc.__class__.__name__, exc, smlt))
    return sorted(set(src_paths))


# ########################## SourceFilterSplitter ########################### #

def filter_and_split(src, sourceprocessor):
    """
    Filter and split the source by using the source processor.
    Also, sets the sub sources `.weight` attribute.

    :param src: a hazardlib source object
    :param sourceprocessor: a SourceFilterSplitter object
    :returns: a named tuple of type SourceInfo
    """
    if sourceprocessor.sitecol:  # filter
        info = sourceprocessor.filter(src)
        if not info.sources:
            return info  # filtered away
        filter_time = info.filter_time
    else:  # only split
        filter_time = 0
    t1 = time.time()
    out = []
    weight_time = 0
    weight = 0
    for ss in sourceconverter.split_source(src, sourceprocessor.asd):
        if sourceprocessor.weight:
            t = time.time()
            ss.weight = get_weight(ss)
            weight_time += time.time() - t
            weight += ss.weight
        out.append(ss)
    src.weight = weight
    split_time = time.time() - t1 - weight_time
    return SourceInfo(src.trt_model_id, src.source_id, src.__class__.__name__,
                      weight, out, filter_time, weight_time, split_time)


SourceInfo = collections.namedtuple(
    'SourceInfo', 'trt_model_id source_id source_class weight sources '
    'filter_time weight_time split_time')

source_info_dt = numpy.dtype(
    [('trt_model_id', numpy.uint32),
     ('source_id', (bytes, 20)),
     ('source_class', (bytes, 20)),
     ('weight', numpy.float32),
     ('split_num', numpy.uint32),
     ('filter_time', numpy.float32),
     ('weight_time', numpy.float32),
     ('split_time', numpy.float32)])


class BaseSourceProcessor(object):
    """
    Do nothing source processor.

    :param sitecol:
        a SiteCollection instance
    :param maxdist:
        maximum distance for the filtering
    :param area_source_discretization:
        area source discretization
    """
    weight = False  # when True, set the weight on each source

    def __init__(self, sitecol, maxdist, area_source_discretization=None):
        self.sitecol = sitecol
        self.maxdist = maxdist
        self.asd = area_source_discretization


class SourceFilter(BaseSourceProcessor):
    """
    Filter sequentially the sources of the given CompositeSourceModel
    instance. An array `.source_info` is added to the instance, containing
    information about the processing times.
    """
    def filter(self, src):
        t0 = time.time()
        sites = src.filter_sites_by_distance_to_source(
            self.maxdist, self.sitecol)
        t1 = time.time()
        filter_time = t1 - t0
        if sites is not None and self.weight:
            t2 = time.time()
            weight = get_weight(src)
            src.weight = weight
            weight_time = time.time() - t2
        else:
            weight = numpy.nan
            weight_time = 0
        sources = [] if sites is None else [src]
        return SourceInfo(
            src.trt_model_id, src.source_id, src.__class__.__name__,
            weight, sources, filter_time, weight_time, 0)

    def agg_source_info(self, acc, info):
        """
        :param acc: a dictionary {trt_model_id: sources}
        :param info: a SourceInfo instance
        """
        self.infos.append(
            SourceInfo(info.trt_model_id, info.source_id, info.source_class,
                       info.weight, len(info.sources), info.filter_time,
                       info.weight_time, info.split_time))
        return acc + {info.trt_model_id: info.sources}

    def process(self, csm, dummy=None):
        """
        :param csm: a CompositeSourceModel instance
        :returns: the times spent in sequential and parallel processing
        """
        sources = csm.get_sources()
        self.infos = []
        seqtime, partime = 0, 0
        sources_by_trt = AccumDict()

        logging.info('Sequential processing of %d sources...', len(sources))
        t1 = time.time()
        for src in sources:
            sources_by_trt = self.agg_source_info(
                sources_by_trt, self.filter(src))
        seqtime = time.time() - t1
        self.update(csm, sources_by_trt)
        return seqtime, partime

    def update(self, csm, sources_by_trt):
        """
        Store the `source_info` array in the composite source model.

        :param csm: a CompositeSourceModel instance
        :param sources_by_trt: a dictionary trt_model_id -> sources
        """
        self.infos.sort(
            key=lambda info: info.filter_time + info.weight_time +
            info.split_time, reverse=True)
        csm.source_info = numpy.array(self.infos, source_info_dt)
        del self.infos[:]

        # update trt_model.sources
        for source_model in csm:
            for trt_model in source_model.trt_models:
                trt_model.sources = sorted(
                    sources_by_trt.get(trt_model.id, []),
                    key=operator.attrgetter('source_id'))
                if not trt_model.sources:
                    logging.warn(
                        'Could not find sources close to the sites in %s '
                        'sm_lt_path=%s, maximum_distance=%s km, TRT=%s',
                        source_model.name, source_model.path,
                        self.maxdist, trt_model.trt)


class SourceFilterWeighter(SourceFilter):
    """
    Filter sequentially the sources of the given CompositeSourceModel
    instance and compute their weights. An array `.source_info` is added
    to the instance, containing information about the processing times.
    """
    weight = True


class SourceFilterSplitter(SourceFilterWeighter):
    """
    Filter and split in parallel the sources of the given CompositeSourceModel
    instance. An array `.source_info` is added to the instance, containing
    information about the processing times and the splitting process.

    :param sitecol: a SiteCollection instance
    :param maxdist: maximum distance for the filtering
    :param area_source_discretization: area source discretization
    """
    def process(self, csm, no_distribute=False):
        """
        :param csm: a CompositeSourceModel instance
        :param no_distribute: flag to disable parallel processing
        :returns: the times spent in sequential and parallel processing
        """
        sources = csm.get_sources()
        fast_sources = [(src, self) for src in sources
                        if src.__class__.__name__ in
                        ('PointSource', 'AreaSource')]
        slow_sources = [(src, self) for src in sources
                        if src.__class__.__name__ not in
                        ('PointSource', 'AreaSource')]
        self.infos = []
        seqtime, partime = 0, 0
        sources_by_trt = AccumDict()

        # start multicore processing
        if slow_sources:
            t0 = time.time()
            logging.warn('Processing %d slow sources...', len(slow_sources))
            with mock.patch.object(
                    parallel, 'no_distribute', lambda: no_distribute):
                ss = parallel.TaskManager.starmap(
                    filter_and_split, slow_sources)

        # single core processing
        if fast_sources:
            logging.info('Processing %d fast sources...', len(fast_sources))
            t1 = time.time()
            sources_by_trt += reduce(
                self.agg_source_info,
                itertools.starmap(filter_and_split, fast_sources), AccumDict())
            seqtime = time.time() - t1

        # finish multicore processing
        sources_by_trt += (ss.reduce(self.agg_source_info)
                           if slow_sources else {})
        if slow_sources:
            partime = time.time() - t0

        self.update(csm, sources_by_trt)

        return seqtime, partime
