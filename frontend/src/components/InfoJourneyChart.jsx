import React, { Fragment, useState } from 'react';
import {
  XYPlot,
  HorizontalGridLines,
  XAxis,
  YAxis,
  VerticalBarSeries,
  ChartLabel,
  Crosshair,
} from 'react-vis';
import DiscreteColorLegend from 'react-vis/dist/legends/discrete-color-legend';
import {
  CHART_COLORS,
  REACT_VIS_CROSSHAIR_NO_LINE,
} from '../UIConstants';
import StartStopIcon from '@material-ui/icons/DirectionsTransit';
import WatchLaterOutlinedIcon from '@material-ui/icons/WatchLaterOutlined';
import '../../node_modules/react-vis/dist/style.css';

/**
 * Bar chart of average and planning percentile wait and time across the day.
 */
function InfoJourneyChart(props) {

  const [crosshairValues, setCrosshairValues] = useState([]); // tooltip starts out empty

  /**
   * Event handler for onMouseLeave.
   * @private
   */
  const onMouseLeave = () => {
    setCrosshairValues([]);
  };

  /**
   * Event handler for onNearestX.
   * @param {Object} value Selected value.
   * @param {index} index Index of the value in the data array.
   * @private
   */
  const onNearestX = (value, event) => {
    console.log(value);
    setCrosshairValues([value]);
  };


  const { firstWaits, secondWaits, firstTravels, secondTravels } = props;

  const legendItems = [
    { title: <Fragment>
               <StartStopIcon fontSize="small" style={{verticalAlign: 'sub'}} />
               &nbsp;Travel
             </Fragment>, color: CHART_COLORS[1], strokeWidth: 10 },
    { title: <Fragment>
               <WatchLaterOutlinedIcon fontSize="small" style={{verticalAlign: 'sub'}} />
               &nbsp;Wait
             </Fragment>, color: CHART_COLORS[0], strokeWidth: 10 },
  ];

    return (
      <div>
        {true ? (
          <div style={{display: 'flex'}}>
            <XYPlot
              xType="ordinal"
              height={125}
              width={175}
              margin={{left: 40, right: 10, top:0, bottom: 30}}
              stackBy="y"
              onMouseLeave={onMouseLeave}
            >
              <HorizontalGridLines />
              <XAxis />
              <YAxis hideLine />

              <VerticalBarSeries
                cluster="first"
                color={CHART_COLORS[0]}
                onValueMouseOver={onNearestX}
                data={[
                  {x: 'Typical', y: firstWaits[0]},
                  {x: 'Planning', y: firstWaits[1]},
                ]}
              />

              <VerticalBarSeries
                cluster="first"
                color={CHART_COLORS[1]}
                onValueMouseOver={onNearestX}
                data={[
                  {x: 'Typical', y: firstTravels[0]},
                  {x: 'Planning', y: firstTravels[1]},
                ]}
              />

              <VerticalBarSeries
                cluster="second"
                color={CHART_COLORS[2]}
                onValueMouseOver={onNearestX}
                data={[
                  {x: 'Typical', y: secondWaits[0]},
                  {x: 'Planning', y: secondWaits[1]},
                ]}
              />

              <VerticalBarSeries
                cluster="second"
                color={CHART_COLORS[3]}
                onValueMouseOver={onNearestX}
                data={[
                  {x: 'Typical', y: secondTravels[0]},
                  {x: 'Planning', y: secondTravels[1]},
                ]}
              />

              <ChartLabel
                text="minutes"
                className="alt-y-label"
                includeMargin={false}
                xPercent={0.00}
                yPercent={0.06}
                style={{
                  transform: 'rotate(-90)',
                  textAnchor: 'end',
                }}
              />

              {crosshairValues.length > 0 && (
                <Crosshair
                  values={crosshairValues}
                  style={REACT_VIS_CROSSHAIR_NO_LINE}
                >
                  <div className="rv-crosshair__inner__content">
                    {Math.round(crosshairValues[0].y - (crosshairValues[0].y0 ? crosshairValues[0].y0 : 0))} min
                  </div>
                </Crosshair>
              )}
            </XYPlot>
            <DiscreteColorLegend
              orientation="vertical"
              width={110}
              items={legendItems}
            />
          </div>
        ) : null}
      </div>
    );
}

export default InfoJourneyChart;