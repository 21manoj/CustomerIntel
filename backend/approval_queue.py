#!/usr/bin/env python3
"""
Approval Queue — Human-in-the-Loop for Agent Actions
======================================================
Tiered approval system:
  - HIGH confidence (>=85%)  → AUTO_EXECUTE (with audit)
  - MEDIUM confidence (60-85%) → PENDING_APPROVAL (notify CSM)
  - LOW confidence (<60%)    → AUTO_REJECT (log reason)

This closes GAP-5 (auto-trigger), GAP-7 (approval workflow),
and works with the agentic loop (agent_loop.py).
"""

import json
import logging
from datetime import datetime
from typing import Dict, List, Optional
from enum import Enum

from extensions import db
from sqlalchemy import Column, Integer, String, Float, Text, DateTime, Boolean, JSON

logger = logging.getLogger(__name__)


# ============================================================
# DATABASE MODEL
# ============================================================

class ApprovalRequest(db.Model):
    """A pending, approved, or rejected agent action request."""
    __tablename__ = "approval_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    customer_id = Column(Integer, nullable=False, index=True)
    account_id = Column(String(128), nullable=False, index=True)
    agent_id = Column(String(64), default="signal_analyst")

    # What the agent wants to do
    action_type = Column(String(64), nullable=False)  # playbook_trigger, notification, escalation
    action_payload = Column(JSON, nullable=True)       # Full action details
    playbook_id = Column(String(128), nullable=True)

    # Why
    predicted_outcome = Column(String(32), nullable=True)   # churn, expansion, stable
    confidence = Column(Float, nullable=False)
    reasoning = Column(Text, nullable=True)
    dollar_impact = Column(Float, nullable=True)

    # Decision
    status = Column(String(32), default="pending", index=True)  # pending, approved, rejected, auto_executed, auto_rejected, expired
    decided_by = Column(String(128), nullable=True)  # user_id or 'system'
    decided_at = Column(DateTime, nullable=True)
    decision_notes = Column(Text, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)


# ============================================================
# APPROVAL QUEUE SERVICE
# ============================================================

class ApprovalQueueService:
    """Manages the approval queue for agent actions."""

    # Thresholds
    AUTO_EXECUTE_THRESHOLD = 0.85
    REVIEW_THRESHOLD = 0.60

    def submit(
        self,
        customer_id: int,
        account_id: str,
        action_type: str,
        confidence: float,
        predicted_outcome: Optional[str] = None,
        reasoning: Optional[str] = None,
        dollar_impact: Optional[float] = None,
        action_payload: Optional[Dict] = None,
        playbook_id: Optional[str] = None,
        agent_id: str = "signal_analyst",
    ) -> Dict:
        """
        Submit an action for approval. Auto-decides based on confidence.

        Returns:
            Dict with request_id, status, and reason.
        """
        request = ApprovalRequest(
            customer_id=customer_id,
            account_id=account_id,
            agent_id=agent_id,
            action_type=action_type,
            action_payload=action_payload,
            playbook_id=playbook_id,
            predicted_outcome=predicted_outcome,
            confidence=confidence,
            reasoning=reasoning,
            dollar_impact=dollar_impact,
        )

        # Tiered decision
        if confidence >= self.AUTO_EXECUTE_THRESHOLD:
            request.status = "auto_executed"
            request.decided_by = "system"
            request.decided_at = datetime.utcnow()
            request.decision_notes = (
                f"Auto-executed: confidence {confidence:.0%} >= {self.AUTO_EXECUTE_THRESHOLD:.0%}"
            )
            self._execute_action(request)

        elif confidence >= self.REVIEW_THRESHOLD:
            request.status = "pending"
            request.decision_notes = (
                f"Pending review: confidence {confidence:.0%} between "
                f"{self.REVIEW_THRESHOLD:.0%}-{self.AUTO_EXECUTE_THRESHOLD:.0%}"
            )

        else:
            request.status = "auto_rejected"
            request.decided_by = "system"
            request.decided_at = datetime.utcnow()
            request.decision_notes = (
                f"Auto-rejected: confidence {confidence:.0%} < {self.REVIEW_THRESHOLD:.0%}"
            )

        db.session.add(request)
        db.session.commit()

        logger.info(
            f"Approval request #{request.id}: {request.status} "
            f"(confidence={confidence:.0%}, action={action_type})"
        )

        return {
            "request_id": request.id,
            "status": request.status,
            "reason": request.decision_notes,
            "dollar_impact": dollar_impact,
        }

    def approve(self, request_id: int, user_id: str, notes: Optional[str] = None) -> Dict:
        """Manually approve a pending request."""
        request = ApprovalRequest.query.get(request_id)
        if not request:
            return {"error": "Request not found"}
        if request.status != "pending":
            return {"error": f"Request is {request.status}, not pending"}

        request.status = "approved"
        request.decided_by = user_id
        request.decided_at = datetime.utcnow()
        request.decision_notes = notes or "Manually approved"
        db.session.commit()

        # Execute the action
        self._execute_action(request)

        return {"request_id": request_id, "status": "approved"}

    def reject(self, request_id: int, user_id: str, notes: Optional[str] = None) -> Dict:
        """Manually reject a pending request."""
        request = ApprovalRequest.query.get(request_id)
        if not request:
            return {"error": "Request not found"}
        if request.status != "pending":
            return {"error": f"Request is {request.status}, not pending"}

        request.status = "rejected"
        request.decided_by = user_id
        request.decided_at = datetime.utcnow()
        request.decision_notes = notes or "Manually rejected"
        db.session.commit()

        return {"request_id": request_id, "status": "rejected"}

    def get_pending(self, customer_id: int, limit: int = 20) -> List[Dict]:
        """Get all pending approval requests for a customer."""
        requests = ApprovalRequest.query.filter_by(
            customer_id=customer_id,
            status="pending",
        ).order_by(ApprovalRequest.created_at.desc()).limit(limit).all()

        return [self._to_dict(r) for r in requests]

    def get_history(self, customer_id: int, limit: int = 50) -> List[Dict]:
        """Get approval history for a customer."""
        requests = ApprovalRequest.query.filter_by(
            customer_id=customer_id,
        ).order_by(ApprovalRequest.created_at.desc()).limit(limit).all()

        return [self._to_dict(r) for r in requests]

    def get_stats(self, customer_id: int) -> Dict:
        """Get approval queue statistics."""
        all_requests = ApprovalRequest.query.filter_by(customer_id=customer_id).all()
        total = len(all_requests)
        if total == 0:
            return {"total": 0, "pending": 0, "auto_executed": 0, "approved": 0, "rejected": 0}

        return {
            "total": total,
            "pending": sum(1 for r in all_requests if r.status == "pending"),
            "auto_executed": sum(1 for r in all_requests if r.status == "auto_executed"),
            "approved": sum(1 for r in all_requests if r.status == "approved"),
            "rejected": sum(1 for r in all_requests if r.status in ("rejected", "auto_rejected")),
            "total_dollar_impact": sum(r.dollar_impact or 0 for r in all_requests if r.status in ("auto_executed", "approved")),
            "avg_confidence": sum(r.confidence for r in all_requests) / total,
        }

    def _execute_action(self, request: ApprovalRequest) -> None:
        """Execute an approved/auto-executed action."""
        try:
            if request.action_type == "playbook_trigger" and request.playbook_id:
                logger.info(
                    f"Executing playbook {request.playbook_id} "
                    f"for account {request.account_id}"
                )
                try:
                    from event_system import EventType, event_manager
                    event_manager.publisher.publish(
                        EventType.PLAYBOOK_AUTO_TRIGGERED,
                        customer_id=request.customer_id,
                        data={
                            "account_id": request.account_id,
                            "playbook_id": request.playbook_id,
                            "confidence": request.confidence,
                            "dollar_impact": request.dollar_impact,
                            "approval_request_id": request.id,
                            # PlaybookAutoTriggerSubscriber iterates data['actions'];
                            # provide the approved playbook as a single-item list so
                            # the orchestrator dispatch actually fires.
                            "actions": [
                                {
                                    "playbook_id": request.playbook_id,
                                    "action": (
                                        f"Approved playbook {request.playbook_id} "
                                        f"for account {request.account_id}"
                                    ),
                                    "dollar_impact": request.dollar_impact,
                                }
                            ],
                        },
                    )
                except Exception as e:
                    logger.warning(f"Failed to publish playbook trigger event: {e}")

            elif request.action_type == "weight_calibration":
                # Wizard C weight change — apply calibrated weights to CustomerConfig
                self._apply_weight_calibration(request)

            elif request.action_type == "notification":
                logger.info(f"Sending notification for account {request.account_id}")

            else:
                logger.info(f"Executing action: {request.action_type}")

        except Exception as e:
            logger.error(f"Failed to execute action {request.id}: {e}")

    def _apply_weight_calibration(self, request: ApprovalRequest) -> None:
        """
        Apply Wizard C calibrated weights from an approved request.

        action_payload schema:
          {
            "scope": "global" | "lifecycle_stage",
            "pillar_weights": {...},
            "kpi_weights": {...},
            "stage_name": "onboarding" (only for lifecycle_stage scope),
            "previous_weights": {...}  (for audit trail)
          }
        """
        from models import CustomerConfig
        payload = request.action_payload or {}
        scope = payload.get('scope', 'global')

        config = CustomerConfig.query.filter_by(
            customer_id=request.customer_id
        ).first()
        if not config:
            logger.error(f"No CustomerConfig for customer {request.customer_id}")
            return

        if scope == 'global':
            # Apply global pillar + KPI weights
            if payload.get('pillar_weights'):
                config.dc2s_pillar_weights = payload['pillar_weights']
            if payload.get('kpi_weights'):
                config.dc2s_kpi_weights = payload['kpi_weights']
            config.customized_by = f'wizard_c_approved_{request.decided_by}'
            logger.info(
                f"Applied Wizard C global weights for customer {request.customer_id} "
                f"(approved by {request.decided_by})"
            )

        elif scope == 'lifecycle_stage':
            # Apply per-stage weights
            stage_name = payload.get('stage_name')
            lc = config.dc2s_lifecycle_stage_weights
            if lc and stage_name:
                import copy
                updated_lc = copy.deepcopy(lc)
                for stage in updated_lc.get('stages', []):
                    if stage.get('name') == stage_name:
                        stage['pillar_weights'] = payload['pillar_weights']
                        break
                config.dc2s_lifecycle_stage_weights = updated_lc
                logger.info(
                    f"Applied Wizard C stage '{stage_name}' weights for customer "
                    f"{request.customer_id} (approved by {request.decided_by})"
                )

        db.session.commit()

    @staticmethod
    def _to_dict(request: ApprovalRequest) -> Dict:
        result = {
            "request_id": request.id,
            "customer_id": request.customer_id,
            "account_id": request.account_id,
            "agent_id": request.agent_id,
            "action_type": request.action_type,
            "action_payload": request.action_payload,
            "playbook_id": request.playbook_id,
            "predicted_outcome": request.predicted_outcome,
            "confidence": request.confidence,
            "reasoning": request.reasoning,
            "dollar_impact": request.dollar_impact,
            "status": request.status,
            "decided_by": request.decided_by,
            "decided_at": request.decided_at.isoformat() if request.decided_at else None,
            "decision_notes": request.decision_notes,
            "created_at": request.created_at.isoformat() if request.created_at else None,
        }

        # Enrich with UUID fields (best-effort)
        try:
            from id_resolve_utils import resolve_customer, resolve_account
            customer = resolve_customer(request.customer_id, allow_none=True)
            if customer:
                result['customer_uuid'] = getattr(customer, 'uuid', None)
            account = resolve_account(request.account_id, allow_none=True)
            if account:
                result['account_uuid'] = getattr(account, 'uuid', None)
        except Exception:
            pass

        return result


# ============================================================
# API BLUEPRINT
# ============================================================

from flask import Blueprint, request as flask_request, jsonify
from auth_middleware import get_current_customer_id

approval_api = Blueprint('approval_api', __name__)
_service = ApprovalQueueService()


@approval_api.route('/api/approvals/pending', methods=['GET'])
def get_pending_approvals():
    """Get all pending approval requests for the current customer."""
    customer_id = get_current_customer_id()
    if not customer_id:
        return jsonify({'error': 'Authentication required'}), 401
    return jsonify({'pending': _service.get_pending(customer_id)})


@approval_api.route('/api/approvals/history', methods=['GET'])
def get_approval_history():
    """Get approval history for the current customer."""
    customer_id = get_current_customer_id()
    if not customer_id:
        return jsonify({'error': 'Authentication required'}), 401
    limit = flask_request.args.get('limit', 50, type=int)
    return jsonify({'history': _service.get_history(customer_id, limit)})


@approval_api.route('/api/approvals/stats', methods=['GET'])
def get_approval_stats():
    """Get approval queue statistics."""
    customer_id = get_current_customer_id()
    if not customer_id:
        return jsonify({'error': 'Authentication required'}), 401
    return jsonify({'stats': _service.get_stats(customer_id)})


@approval_api.route('/api/approvals/<int:request_id>/approve', methods=['POST'])
def approve_request(request_id):
    """Manually approve a pending request."""
    customer_id = get_current_customer_id()
    if not customer_id:
        return jsonify({'error': 'Authentication required'}), 401

    from auth_middleware import get_current_user_id
    user_id = get_current_user_id() or str(customer_id)
    notes = (flask_request.json or {}).get('notes')

    result = _service.approve(request_id, user_id, notes)
    if 'error' in result:
        return jsonify(result), 400
    return jsonify(result)


@approval_api.route('/api/approvals/<int:request_id>/reject', methods=['POST'])
def reject_request(request_id):
    """Manually reject a pending request."""
    customer_id = get_current_customer_id()
    if not customer_id:
        return jsonify({'error': 'Authentication required'}), 401

    from auth_middleware import get_current_user_id
    user_id = get_current_user_id() or str(customer_id)
    notes = (flask_request.json or {}).get('notes')

    result = _service.reject(request_id, user_id, notes)
    if 'error' in result:
        return jsonify(result), 400
    return jsonify(result)
