#  -*- coding: utf-8 -*-
#  vim: tabstop=4 shiftwidth=4 softtabstop=4

#  Copyright (c) 2015, GEM Foundation

#  OpenQuake is free software: you can redistribute it and/or modify it
#  under the terms of the GNU Affero General Public License as published
#  by the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.

#  OpenQuake is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.

#  You should have received a copy of the GNU Affero General Public License
#  along with OpenQuake.  If not, see <http://www.gnu.org/licenses/>.

import logging
import operator
import collections

import numpy

from openquake.baselib.general import AccumDict, humansize
from openquake.calculators import base
from openquake.commonlib import readinput, parallel, datastore
from openquake.risklib import riskinput, scientific
from openquake.commonlib.parallel import apply_reduce

OUTPUTS = ['event_loss_table-rlzs', 'specific_losses-rlzs',
           'rcurves-rlzs', 'insured_rcurves-rlzs']

AGGLOSS, SPECLOSS, RC, IC = 0, 1, 2, 3

elt_dt = numpy.dtype([('rup_id', numpy.uint32), ('loss', numpy.float32),
                      ('ins_loss',  numpy.float32)])
ela_dt = numpy.dtype([('rup_id', numpy.uint32), ('ass_id', numpy.uint32),
                      ('loss', numpy.float32), ('ins_loss',  numpy.float32)])


def cube(O, L, R, factory):
    """
    :param O: the number of different outputs
    :param L: the number of loss types
    :param R: the number of realizations
    :param factory: thunk used to initialize the elements
    :returns: a numpy array of shape (O, L, R)
    """
    losses = numpy.zeros((O, L, R), object)
    for o in range(O):
        for l in range(L):
            for r in range(R):
                losses[o, l, r] = factory()
    return losses


@parallel.litetask
def ebr(riskinputs, riskmodel, rlzs_assoc, monitor):
    """
    :param riskinputs:
        a list of :class:`openquake.risklib.riskinput.RiskInput` objects
    :param riskmodel:
        a :class:`openquake.risklib.riskinput.RiskModel` instance
    :param rlzs_assoc:
        a class:`openquake.commonlib.source.RlzsAssoc` instance
    :param monitor:
        :class:`openquake.commonlib.parallel.PerformanceMonitor` instance
    :returns:
        a numpy array of shape (O, L, R); each element is a list containing
        a single array of dtype elt_dt, or an empty list
    """
    lti = riskmodel.lti  # loss type -> index
    specific_assets = monitor.oqparam.specific_assets
    result = cube(
        monitor.num_outputs, len(lti), len(rlzs_assoc.realizations), list)
    for out_by_rlz in riskmodel.gen_outputs(riskinputs, rlzs_assoc, monitor):
        rup_slice = out_by_rlz.rup_slice
        rup_ids = list(range(rup_slice.start, rup_slice.stop))
        for out in out_by_rlz:
            l = lti[out.loss_type]
            asset_ids = [a.idx for a in out.assets]

            # collect losses for specific assets
            specific_ids = set(a.idx for a in out.assets
                               if a.id in specific_assets)
            if specific_ids:
                for rup_id, all_losses, ins_losses in zip(
                        rup_ids, out.event_loss_per_asset,
                        out.insured_loss_per_asset):
                    for aid, sloss, iloss in zip(
                            asset_ids, all_losses, ins_losses):
                        if aid in specific_ids:
                            if sloss > 0:
                                result[SPECLOSS, l, out.hid].append(
                                    (rup_id, aid, sloss, iloss))

            # collect aggregate losses
            agg_losses = out.event_loss_per_asset.sum(axis=1)
            agg_ins_losses = out.insured_loss_per_asset.sum(axis=1)
            for rup_id, loss, ins_loss in zip(
                    rup_ids, agg_losses, agg_ins_losses):
                if loss > 0:
                    result[AGGLOSS, l, out.hid].append(
                        (rup_id, numpy.array([loss, ins_loss])))

            # dictionaries asset_idx -> array of counts
            if len(riskmodel.curve_builders[l].ratios):
                result[RC, l, out.hid].append(dict(
                    zip(asset_ids, out.counts_matrix)))
                if out.insured_counts_matrix.sum():
                    result[IC, l, out.hid].append(dict(
                        zip(asset_ids, out.insured_counts_matrix)))

    for idx, lst in numpy.ndenumerate(result):
        o, l, r = idx
        if lst:
            if o == AGGLOSS:
                acc = collections.defaultdict(float)
                for rupt, loss in lst:
                    acc[rupt] += loss
                result[idx] = [numpy.array([(rup, loss[0], loss[1])
                                            for rup, loss in acc.items()],
                                           elt_dt)]
            elif o == SPECLOSS:
                result[idx] = [numpy.array(lst, ela_dt)]
            else:  # risk curves
                result[idx] = [sum(lst, AccumDict())]
        else:
            result[idx] = []
    return result


@base.calculators.add('ebr')
class EventBasedRiskCalculator(base.RiskCalculator):
    """
    Event based PSHA calculator generating the event loss table and
    fixed ratios loss curves.
    """
    pre_calculator = 'event_based_rupture'
    core_func = ebr

    epsilon_matrix = datastore.persistent_attribute('epsilon_matrix')
    is_stochastic = True

    def pre_execute(self):
        """
        Read the precomputed ruptures (or compute them on the fly) and
        prepare some datasets in the datastore.
        """
        super(EventBasedRiskCalculator, self).pre_execute()
        if not self.riskmodel:  # there is no riskmodel, exit early
            self.execute = lambda: None
            self.post_execute = lambda result: None
            return
        oq = self.oqparam
        epsilon_sampling = oq.epsilon_sampling
        correl_model = readinput.get_correl_model(oq)
        gsims_by_col = self.rlzs_assoc.get_gsims_by_col()
        assets_by_site = self.assets_by_site
        # the following is needed to set the asset idx attribute
        self.assetcol = riskinput.build_asset_collection(
            assets_by_site, oq.time_event)

        logging.info('Populating the risk inputs')
        rup_by_tag = sum(self.datastore['sescollection'], AccumDict())
        all_ruptures = [rup_by_tag[tag] for tag in sorted(rup_by_tag)]
        num_samples = min(len(all_ruptures), epsilon_sampling)
        eps_dict = riskinput.make_eps_dict(
            assets_by_site, num_samples, oq.master_seed, oq.asset_correlation)
        logging.info('Generated %d epsilons', num_samples * len(eps_dict))
        self.epsilon_matrix = numpy.array(
            [eps_dict[a['asset_ref']] for a in self.assetcol])
        self.riskinputs = list(self.riskmodel.build_inputs_from_ruptures(
            self.sitecol.complete, all_ruptures, gsims_by_col,
            oq.truncation_level, correl_model, eps_dict,
            oq.concurrent_tasks or 1))
        logging.info('Built %d risk inputs', len(self.riskinputs))

        # preparing empty datasets
        loss_types = self.riskmodel.loss_types
        self.L = len(loss_types)
        self.R = len(self.rlzs_assoc.realizations)
        self.outs = OUTPUTS
        self.datasets = {}
        self.monitor.oqparam = self.oqparam
        # ugly: attaching an attribute needed in the task function
        self.monitor.num_outputs = len(self.outs)
        # attaching two other attributes used in riskinput.gen_outputs
        self.monitor.assets_by_site = self.assets_by_site
        self.monitor.num_assets = N = self.count_assets()
        for o, out in enumerate(self.outs):
            self.datastore.hdf5.create_group(out)
            for l, loss_type in enumerate(loss_types):
                cb = self.riskmodel.curve_builders[l]
                C = len(cb.ratios)  # curve resolution
                for r, rlz in enumerate(self.rlzs_assoc.realizations):
                    key = '/%s/rlz-%03d' % (loss_type, rlz.ordinal)
                    if o == AGGLOSS:  # loss tables
                        dset = self.datastore.create_dset(out + key, elt_dt)
                    elif o == SPECLOSS:  # specific losses
                        dset = self.datastore.create_dset(out + key, ela_dt)
                    else:  # risk curves
                        if not C:
                            continue
                        dset = self.datastore.create_dset(
                            out + key, numpy.float32, (N, C))
                    self.datasets[o, l, r] = dset
                if o == RC and C:
                    grp = self.datastore['%s/%s' % (out, loss_type)]
                    grp.attrs['loss_ratios'] = cb.ratios

    def execute(self):
        """
        Run the ebr calculator in parallel and aggregate the results
        """
        return apply_reduce(
            self.core_func.__func__,
            (self.riskinputs, self.riskmodel, self.rlzs_assoc, self.monitor),
            concurrent_tasks=self.oqparam.concurrent_tasks,
            agg=self.agg,
            acc=cube(self.monitor.num_outputs, self.L, self.R, list),
            weight=operator.attrgetter('weight'),
            key=operator.attrgetter('col_id'))

    def agg(self, acc, result):
        """
        Aggregate list of arrays in longer lists.

        :param acc: accumulator array of shape (O, L, R)
        :param result: a numpy array of shape (O, L, R)
        """
        for idx, arrays in numpy.ndenumerate(result):
            acc[idx].extend(arrays)
        return acc

    def post_execute(self, result):
        """
        Save the event loss table in the datastore.

        :param result:
            a numpy array of shape (O, L, R) containing lists of arrays
        """
        nses = self.oqparam.ses_per_logic_tree_path
        saved = {out: 0 for out in self.outs}
        N = len(self.assetcol)
        with self.monitor('saving loss table',
                          autoflush=True, measuremem=True):
            for (o, l, r), data in numpy.ndenumerate(result):
                if not data:  # empty list
                    continue
                if o in (AGGLOSS, SPECLOSS):  # data is a list of arrays
                    losses = numpy.concatenate(data)
                    self.datasets[o, l, r].extend(losses)
                    saved[self.outs[o]] += losses.nbytes
                else:  # risk curves, data is a list of counts dictionaries
                    cb = self.riskmodel.curve_builders[l]
                    counts_matrix = cb.get_counts(N, data)
                    poes = scientific.build_poes(counts_matrix, nses)
                    self.datasets[o, l, r].dset[:] = poes
                    saved[self.outs[o]] += poes.nbytes
                self.datastore.hdf5.flush()

        for out in self.outs:
            nbytes = saved[out]
            if nbytes:
                self.datastore[out].attrs['nbytes'] = nbytes
                logging.info('Saved %s in %s', humansize(nbytes), out)
            else:  # remove empty outputs
                del self.datastore[out]