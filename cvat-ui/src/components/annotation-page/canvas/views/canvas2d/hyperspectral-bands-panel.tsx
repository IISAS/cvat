// Copyright (C) CVAT.ai Corporation
//
// SPDX-License-Identifier: MIT

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import { Row, Col } from 'antd/lib/grid';
import Select from 'antd/lib/select';
import InputNumber from 'antd/lib/input-number';
import Button from 'antd/lib/button';
import Text from 'antd/lib/typography/Text';

import { getCore } from 'cvat-core-wrapper';
import { changeFrameAsync } from 'actions/annotation-actions';
import { CombinedState } from 'reducers';

const core = getCore();

interface HyperspectralFrameMeta {
    frame: number;
    band_count: number;
    default_r_band: number;
    default_g_band: number;
    default_b_band: number;
    wavelengths: number[] | null;
}

interface BandState {
    rBand: number;
    gBand: number;
    bBand: number;
    stretchLo: number;
    stretchHi: number;
}

const DEFAULT_STRETCH: [number, number] = [2, 98];

function storageKey(jobID: number): string {
    return `cvat.hyperspectral.bands.job.${jobID}`;
}

function loadPersisted(jobID: number): BandState | null {
    try {
        const raw = window.localStorage.getItem(storageKey(jobID));
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        if (
            typeof parsed.rBand === 'number' &&
            typeof parsed.gBand === 'number' &&
            typeof parsed.bBand === 'number' &&
            typeof parsed.stretchLo === 'number' &&
            typeof parsed.stretchHi === 'number'
        ) {
            return parsed;
        }
    } catch {
        // fall through to null
    }
    return null;
}

function persist(jobID: number, state: BandState): void {
    try {
        window.localStorage.setItem(storageKey(jobID), JSON.stringify(state));
    } catch {
        // storage quota / private-mode — silently tolerate
    }
}

interface Props {
    jobID: number;
}

// Label rendering is split out so the same formatting is used in selected
// labels (collapsed dropdown) and in each option row.
function formatBandLabel(bandIdx: number, wavelengths: number[] | null): string {
    if (wavelengths && Number.isFinite(wavelengths[bandIdx])) {
        return `Band ${bandIdx} (${wavelengths[bandIdx].toFixed(1)} nm)`;
    }
    return `Band ${bandIdx}`;
}

export default function HyperspectralBandsPanel(props: Props): JSX.Element | null {
    const { jobID } = props;
    const dispatch = useDispatch();
    const currentFrame = useSelector((state: CombinedState) => state.annotation.player.frame.number);
    const canvasInstance = useSelector((state: CombinedState) => state.annotation.canvas.instance);
    const [meta, setMeta] = useState<HyperspectralFrameMeta | null>(null);
    const [loaded, setLoaded] = useState(false);
    const [state, setState] = useState<BandState | null>(null);
    const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    // Force a re-fetch of the current frame whenever bands change. cvat-core
    // already cleared the decoded-chunk cache; this triggers the actual
    // network round-trip + canvas redraw.
    //
    // Without forceFrameUpdate the canvas short-circuits setup() when the
    // target frame number equals the currently displayed one — see
    // canvasModel.ts:558. changeFrameAsync fetches new FrameData but the
    // canvas refuses to draw it until forceFrameUpdate flips.
    const refetchCurrentFrame = (): void => {
        if (canvasInstance && 'configure' in canvasInstance) {
            canvasInstance.configure({ forceFrameUpdate: true });
        }
        dispatch(changeFrameAsync(currentFrame, undefined, undefined, true));
    };

    // Fetch per-frame meta once per job; use the first scene to seed defaults
    // and populate band dropdowns. All scenes in a task share the picker —
    // mismatched band counts are handled by clamping in the server.
    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const response = await core.frames.getHyperspectralMeta(jobID);
                if (cancelled) return;
                const frames: HyperspectralFrameMeta[] = response.frames || [];
                if (frames.length === 0) {
                    setMeta(null);
                    setLoaded(true);
                    return;
                }
                const first = frames[0];
                setMeta(first);

                const persisted = loadPersisted(jobID);
                const initial: BandState = persisted ?? {
                    rBand: first.default_r_band,
                    gBand: first.default_g_band,
                    bBand: first.default_b_band,
                    stretchLo: DEFAULT_STRETCH[0],
                    stretchHi: DEFAULT_STRETCH[1],
                };
                setState(initial);
                setLoaded(true);

                // If persisted state diverges from server defaults, apply it
                // so the first frame rendered matches what the user had.
                if (persisted) {
                    await core.frames.setHyperspectralBands(jobID, persisted);
                    refetchCurrentFrame();
                }
            } catch {
                if (!cancelled) {
                    setMeta(null);
                    setLoaded(true);
                }
            }
        })();
        return () => {
            cancelled = true;
        };
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [jobID]);

    const bandOptions = useMemo(() => {
        if (!meta) return [];
        return Array.from({ length: meta.band_count }, (_, i) => ({
            value: i,
            label: formatBandLabel(i, meta.wavelengths),
        }));
    }, [meta]);

    function scheduleApply(next: BandState): void {
        setState(next);
        persist(jobID, next);
        if (debounceRef.current) clearTimeout(debounceRef.current);
        debounceRef.current = setTimeout(async () => {
            await core.frames.setHyperspectralBands(jobID, next);
            refetchCurrentFrame();
        }, 150);
    }

    function resetToDefaults(): void {
        if (!meta) return;
        const next: BandState = {
            rBand: meta.default_r_band,
            gBand: meta.default_g_band,
            bBand: meta.default_b_band,
            stretchLo: DEFAULT_STRETCH[0],
            stretchHi: DEFAULT_STRETCH[1],
        };
        scheduleApply(next);
        // Also clear any custom state on the server so default-band chunks
        // (pre-built at task creation) are hit instead of re-rendered.
        window.localStorage.removeItem(storageKey(jobID));
        core.frames.setHyperspectralBands(jobID, null);
        refetchCurrentFrame();
    }

    if (!loaded) return null;
    if (!meta || !state) return null; // non-hyperspectral job

    return (
        <div className='cvat-canvas-hyperspectral-bands'>
            <Text strong>Hyperspectral bands</Text>
            <hr />
            <Row gutter={8} align='middle'>
                <Col span={4}><Text>Red</Text></Col>
                <Col span={20}>
                    <Select
                        style={{ width: '100%' }}
                        value={state.rBand}
                        options={bandOptions}
                        showSearch
                        optionFilterProp='label'
                        onChange={(value) => scheduleApply({ ...state, rBand: value })}
                    />
                </Col>
            </Row>
            <Row gutter={8} align='middle' style={{ marginTop: 4 }}>
                <Col span={4}><Text>Green</Text></Col>
                <Col span={20}>
                    <Select
                        style={{ width: '100%' }}
                        value={state.gBand}
                        options={bandOptions}
                        showSearch
                        optionFilterProp='label'
                        onChange={(value) => scheduleApply({ ...state, gBand: value })}
                    />
                </Col>
            </Row>
            <Row gutter={8} align='middle' style={{ marginTop: 4 }}>
                <Col span={4}><Text>Blue</Text></Col>
                <Col span={20}>
                    <Select
                        style={{ width: '100%' }}
                        value={state.bBand}
                        options={bandOptions}
                        showSearch
                        optionFilterProp='label'
                        onChange={(value) => scheduleApply({ ...state, bBand: value })}
                    />
                </Col>
            </Row>
            <Row gutter={8} align='middle' style={{ marginTop: 8 }}>
                <Col span={12}>
                    <Text>Stretch lo %</Text>
                    <InputNumber
                        style={{ width: '100%' }}
                        min={0}
                        max={state.stretchHi - 0.1}
                        step={0.5}
                        value={state.stretchLo}
                        onChange={(value) => {
                            if (typeof value !== 'number') return;
                            scheduleApply({ ...state, stretchLo: value });
                        }}
                    />
                </Col>
                <Col span={12}>
                    <Text>Stretch hi %</Text>
                    <InputNumber
                        style={{ width: '100%' }}
                        min={state.stretchLo + 0.1}
                        max={100}
                        step={0.5}
                        value={state.stretchHi}
                        onChange={(value) => {
                            if (typeof value !== 'number') return;
                            scheduleApply({ ...state, stretchHi: value });
                        }}
                    />
                </Col>
            </Row>
            <Row justify='end' style={{ marginTop: 8 }}>
                <Button size='small' onClick={resetToDefaults}>Reset to HDR defaults</Button>
            </Row>
        </div>
    );
}
