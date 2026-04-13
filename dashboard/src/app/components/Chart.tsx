import React, { useEffect, useRef, useState, useImperativeHandle, forwardRef } from 'react';
import { createChart, ColorType, IChartApi, ISeriesApi, LineData, CandlestickData, CandlestickSeries, LineSeries } from 'lightweight-charts';
import { format } from 'date-fns';

export interface ChartProps {
  data: CandlestickData[] | LineData[];
  type?: 'candlestick' | 'line';
  color?: string;
  height?: number;
  markers?: Array<{ time: any; position: 'aboveBar' | 'belowBar'; color: string; shape: 'arrowUp' | 'arrowDown'; text: string }>;
}

export const Chart = forwardRef((props: ChartProps, ref) => {
  const { data, type = 'candlestick', color = '#4a9eff', height = 400, markers: externalMarkers } = props;
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<any> | null>(null);

  useImperativeHandle(ref, () => ({
    updateData: (newData: any) => {
      if (seriesRef.current) {
        seriesRef.current.update(newData);
      }
    },
    setMarkers: (newMarkers: any[]) => {
      if (seriesRef.current) {
        // @ts-ignore
        seriesRef.current.setMarkers(newMarkers);
      }
    }
  }));

  useEffect(() => {
    if (!chartContainerRef.current) return;

    const chart = createChart(chartContainerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: '#888888',
      },
      grid: {
        vertLines: { color: 'rgba(255, 255, 255, 0.05)' },
        horzLines: { color: 'rgba(255, 255, 255, 0.05)' },
      },
      crosshair: {
        mode: 1, // Normal crosshair
        vertLine: { color: 'rgba(255, 255, 255, 0.2)', width: 1, style: 1 },
        horzLine: { color: 'rgba(255, 255, 255, 0.2)', width: 1, style: 1 },
      },
      rightPriceScale: {
        borderColor: 'rgba(255, 255, 255, 0.1)',
      },
      timeScale: {
        borderColor: 'rgba(255, 255, 255, 0.1)',
        timeVisible: true,
        secondsVisible: false,
      },
      handleScroll: {
        mouseWheel: true,
        pressedMouseMove: true,
      },
      handleScale: {
        axisPressedMouseMove: true,
        mouseWheel: true,
        pinch: true,
      },
    });

    chartRef.current = chart;

    let series;
    if (type === 'candlestick') {
      series = chart.addSeries(CandlestickSeries, {
        upColor: '#00d4aa',
        downColor: '#ff4466',
        borderVisible: false,
        wickUpColor: '#00d4aa',
        wickDownColor: '#ff4466',
      });
    } else {
      series = chart.addSeries(LineSeries, {
        color: color,
        lineWidth: 2,
        crosshairMarkerVisible: true,
      });
    }

    seriesRef.current = series;
    series.setData(data as any);

    if (externalMarkers && externalMarkers.length > 0) {
      series.setMarkers(externalMarkers);
    }

    const handleResize = () => {
      if (chartContainerRef.current) {
        chart.applyOptions({ width: chartContainerRef.current.clientWidth });
      }
    };

    window.addEventListener('resize', handleResize);
    chart.timeScale().fitContent();

    return () => {
      window.removeEventListener('resize', handleResize);
      chart.remove();
    };
  }, [type]); // Re-create chart only if type changes

  useEffect(() => {
    if (seriesRef.current && data && data.length > 0) {
      seriesRef.current.setData(data as any);
      if (externalMarkers && externalMarkers.length > 0) {
        // @ts-ignore
        seriesRef.current.setMarkers(externalMarkers);
      }
    }
  }, [data, externalMarkers]);

  return (
    <div className="w-full relative" style={{ height }}>
      <div 
        ref={chartContainerRef} 
        className="absolute inset-0"
      />
    </div>
  );
});

Chart.displayName = 'Chart';
