# SPDX-License-Identifier: Apache-2.0
#
# Copyright (C) 2019, Arm Limited and contributors.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import os
import os.path
import abc

from lisa.wlgen.rta import RTA, Periodic
from lisa.tests.base import TestBundle, Result, ResultBundle, RTATestBundle
from lisa.trace import Trace, FtraceCollector, FtraceConf, requires_events
from lisa.target import Target
from lisa.utils import ArtifactPath
from lisa.analysis.frequency import FrequencyAnalysis

class SchedTuneItemBase(RTATestBundle):
    """
    Abstract class enabling rtapp execution in a schedtune group

    :param boost: The boost level to set for the cgroup
    :type boost: int

    :param prefer_idle: The prefer_idle flag to set for the cgroup
    :type prefer_idle: bool
    """

    def __init__(self, res_dir, plat_info, boost, prefer_idle):
        super().__init__(res_dir, plat_info)
        self.boost = boost
        self.prefer_idle = prefer_idle

    @property
    def cgroup_configuration(self):
        return self.get_cgroup_configuration(self.plat_info, self.boost, self.prefer_idle)

    @classmethod
    def get_cgroup_configuration(cls, plat_info, boost, prefer_idle):
        attributes = {
                    'boost': boost,
                    'prefer_idle': int(prefer_idle)
                 }
        return { 'name': 'lisa_test',
                 'controller': 'schedtune',
                 'attributes': attributes }

    @classmethod
    # Not annotated, to prevent exekall from picking it up. See
    # SchedTuneBase.from_target
    def from_target(cls, target, boost, prefer_idle, res_dir=None, ftrace_coll=None):
        """
        .. warning:: `res_dir` is at the end of the parameter list, unlike most
            other `from_target` where it is the second one.
        """
        return super().from_target(target, res_dir, boost=boost,
                prefer_idle=prefer_idle, ftrace_coll=ftrace_coll)

    @classmethod
    def _from_target(cls, target, res_dir, boost, prefer_idle, ftrace_coll=None):
        plat_info = target.plat_info
        rtapp_profile = cls.get_rtapp_profile(plat_info)
        cgroup_config = cls.get_cgroup_configuration(plat_info, boost, prefer_idle)
        cls._run_rtapp(target, res_dir, rtapp_profile, ftrace_coll, cgroup_config)

        return cls(res_dir, plat_info, boost, prefer_idle)

class SchedTuneBase(TestBundle):
    """
    Abstract class enabling the aggregation of ``SchedTuneItemBase``

    :param test_bundles: a list of test bundles generated by
        multiple ``SchedTuneItemBase`` instances
    :type test_bundles: list
    """
    def __init__(self, res_dir, plat_info, test_bundles):
        super().__init__(res_dir, plat_info)

        self.test_bundles = test_bundles

    @classmethod
    def from_target(cls, target:Target, res_dir:ArtifactPath=None,
            ftrace_coll:FtraceCollector=None) -> 'SchedTuneBase':
        """
        Creates a SchedTuneBase bundle from the target.
        """
        return super().from_target(target, res_dir, ftrace_coll=ftrace_coll)

    @classmethod
    def _from_target(cls, target, res_dir, ftrace_coll):
        return cls(res_dir, target.plat_info,
            list(cls._create_test_bundles(target, res_dir, ftrace_coll))
        )

    @classmethod
    @abc.abstractmethod
    def _create_test_bundles(cls, target, res_dir, ftrace_coll):
        """
        Collects and yields a ResultBundle per test item.
        """
        pass

    @classmethod
    def _create_test_bundle_item(cls, target, res_dir, ftrace_coll, item_cls,
                                 boost, prefer_idle):
        """
        Creates and returns a TestBundle for a given item class, and a given
        schedtune configuration
        """
        item_dir = ArtifactPath.join(res_dir, 'boost_{}_prefer_idle_{}'.format(
                                     boost, int(prefer_idle)))
        os.makedirs(item_dir)

        logger = cls.get_logger()
        logger.info('Running {} with boost={}, prefer_idle={}'.format(
                    item_cls.__name__, boost, prefer_idle))
        return item_cls.from_target(target, boost, prefer_idle, res_dir=item_dir, ftrace_coll=ftrace_coll)

    def _merge_res_bundles(self, res_bundles):
        """
        Merge a set of result bundles
        """
        overall_bundle = ResultBundle.from_bool(all(res_bundles.values()))
        for name, bundle in res_bundles.items():
            overall_bundle.add_metric(name, bundle.metrics)

        overall_bundle.add_metric('failed', [
            name for name, bundle in res_bundles.items()
            if bundle.result is Result.FAILED
        ])
        return overall_bundle

class SchedTuneFreqItem(SchedTuneItemBase):
    """
    Runs a tiny RT rtapp task pinned to a big CPU at a given boost level and
    checks the frequency selection was performed accordingly.
    """

    @classmethod
    def get_rtapp_profile(cls, plat_info):
        cpu = plat_info['capacity-classes'][-1][0]
        rtapp_profile = {}
        rtapp_profile['rta_stune'] = Periodic(
            duty_cycle_pct = 1, # very small task, no impact on freq w/o boost
            duration_s = 10,
            period_ms = 16,
            cpus = [cpu], # pin to big CPU, to focus on frequency selection
            sched_policy = 'FIFO' # RT tasks have the boost holding feature so
                                  # the frequency should be more stable, and we
                                  # shouldn't go to max freq in Android
        )
        return rtapp_profile

    @requires_events(SchedTuneItemBase.trace_window.used_events, "cpu_frequency")
    def trace_window(self, trace):
        """
        Set the boundaries of the trace window to ``cpu_frequency`` events
        before/after the task's start/end time
        """
        rta_start, rta_stop = super().trace_window(trace)

        cpu = self.plat_info['capacity-classes'][-1][0]
        freq_df = trace.df_events('cpu_frequency')
        freq_df = freq_df[freq_df.cpu == cpu]

        # Find the frequency events before and after the task runs
        freq_start = freq_df[freq_df.index < rta_start].index[-1]
        freq_stop = freq_df[freq_df.index > rta_stop].index[0]

        return (freq_start, freq_stop)

    @FrequencyAnalysis.get_average_cpu_frequency.used_events
    def test_stune_frequency(self, freq_margin_pct=10) -> ResultBundle:
        """
        Test that frequency selection followed the boost

        :param: freq_margin_pct: Allowed margin between estimated and measured
            average frequencies
        :type freq_margin_pct: int

        Compute the expected frequency given the boost level and compare to the
        real average frequency from the trace.
        Check that the difference between expected and measured frequencies is
        no larger than ``freq_margin_pct``.
        """
        kernel_version = self.plat_info['kernel']['version']
        if kernel_version.parts[:2] < (4, 14):
            self.get_logger().warning('This test requires the RT boost hold, but it may be disabled in {}'.format(kernel_version))

        cpu = self.plat_info['capacity-classes'][-1][0]
        freqs = self.plat_info['freqs'][cpu]
        max_freq = max(freqs)

        # Estimate the target frequency, including sugov's margin, and round
        # into a real OPP
        boost = self.boost
        target_freq = min(max_freq, max_freq * boost / 80)
        target_freq = list(filter(lambda f: f >= target_freq, freqs))[0]

        # Get the real average frequency
        avg_freq = self.trace.analysis.frequency.get_average_cpu_frequency(cpu)

        distance = abs(target_freq - avg_freq) * 100 / target_freq
        res = ResultBundle.from_bool(distance < freq_margin_pct)
        res.add_metric("target freq", target_freq, 'kHz')
        res.add_metric("average freq", avg_freq, 'kHz')

        return res

class SchedTuneFrequencyTest(SchedTuneBase):
    """
    Runs multiple ``SchedTuneFreqItem`` tests at various boost levels ranging
    from 20% to 100%, then checks all succedeed.
    """

    # Make sure exekall will always collect all events required by items
    ftrace_conf = SchedTuneFreqItem.ftrace_conf

    @classmethod
    def _create_test_bundles(cls, target, res_dir, ftrace_coll):
        for boost in range(20, 101, 20):
            yield cls._create_test_bundle_item(target, res_dir, ftrace_coll,
                    SchedTuneFreqItem, boost, False)

    def test_stune_frequency(self, freq_margin_pct=10) -> ResultBundle:
        """
        .. seealso:: :meth:`SchedTuneFreqItem.test_stune_frequency`
        """
        res_bundles = {
                'boost{}'.format(b.boost): b.test_stune_frequency(freq_margin_pct)
                for b in self.test_bundles
        }
        return self._merge_res_bundles(res_bundles)

# vim :set tabstop=4 shiftwidth=4 textwidth=80 expandtab
